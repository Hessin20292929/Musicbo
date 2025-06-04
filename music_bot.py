import discord
from discord.ext import commands, tasks
import asyncio
import yt_dlp
import os
from collections import deque
import datetime # For formatting duration
import logging # For more structured logging

# --- Basic Logging Setup ---
# This helps differentiate bot instances if multiple are running by mistake
# and provides more structured output than just print()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - PID:%(process)d - %(message)s')
logger = logging.getLogger(__name__)


# Optional: If you are using a .env file for your token and other settings
try:
    from dotenv import load_dotenv
    load_dotenv() # Loads variables from .env into os.environ
    logger.info(".env file loaded if present.")
except ImportError:
    logger.info(".env library (python-dotenv) not found, assuming environment variables are set directly.")


# --- PyNaCl Check (discord.py loads it, this is just an explicit import) ---
try:
    import nacl
    logger.info("PyNaCl library successfully imported.")
except ImportError:
    logger.error("PyNaCl library NOT FOUND. Voice functionality will not work. "
                 "Please install it with 'pip install PyNaCl' and ensure libsodium is available on your system.")
    # exit() # Optionally exit if PyNaCl is critical (discord.py will error later anyway)


# --- Bot Configuration ---
BOT_TOKEN = os.getenv("DISCORD_MUSIC_BOT_TOKEN")

if not BOT_TOKEN:
    logger.critical("CRITICAL: Bot token not found. Please set the DISCORD_MUSIC_BOT_TOKEN environment variable.")
    exit()

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
logger.info(f"Using FFmpeg path: {FFMPEG_PATH}")

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'  # No video, audio only
}

YDL_OPTIONS = {
    'format': 'bestaudio/best', # Choose best audio format
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s', # Output template
    'restrictfilenames': True,
    'noplaylist': True,        # When a playlist URL is given, only download the first item.
                               # For search, yt-dlp might still return a list of 'entries'.
    'nocheckcertificate': True,
    'ignoreerrors': False,     # If True, yt-dlp continues on download errors (e.g. for playlists)
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch1', # Prepend 'ytsearch1:' to queries to get the first YouTube search result.
                                 # 'ytsearch:' would also work, 'ytsearch1:' is more explicit for "first result".
    'source_address': '0.0.0.0'  # Fix for some IPv6 issues
}

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="m!", intents=intents)

# --- Per-Guild State Management ---
music_queues = {}  # guild_id: deque of song_info dictionaries
current_song_info = {}  # guild_id: song_info dictionary for the currently playing song
guild_volumes = {} # guild_id: float (volume level, 0.0 to 2.0)


# --- Helper Functions ---
async def ensure_voice(ctx: commands.Context):
    logger.debug(f"ensure_voice called in guild {ctx.guild.id} by {ctx.author.name} (PID: {os.getpid()})")
    if not ctx.author.voice:
        await ctx.send("You are not connected to a voice channel.")
        return None

    user_channel = ctx.author.voice.channel
    if not ctx.voice_client: # Bot is not connected to any voice channel in this guild
        try:
            logger.info(f"Bot connecting to voice channel: {user_channel.name} (ID: {user_channel.id}) in guild {ctx.guild.id} (PID: {os.getpid()})")
            vc = await user_channel.connect()
            return vc
        except discord.ClientException as e:
            logger.error(f"Error connecting to voice channel {user_channel.id} (PID: {os.getpid()}): {e}")
            await ctx.send(f"Error connecting to voice channel: {e}")
            return None
    
    if ctx.voice_client.channel == user_channel:
        return ctx.voice_client # Already in the correct channel
    
    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
        try:
            logger.info(f"Bot moving to voice channel: {user_channel.name} (ID: {user_channel.id}) in guild {ctx.guild.id} (PID: {os.getpid()})")
            await ctx.voice_client.move_to(user_channel)
            await ctx.send(f"Moved to **{user_channel.name}**.")
            return ctx.voice_client
        except Exception as e:
            logger.error(f"Could not move bot to channel {user_channel.id} (PID: {os.getpid()}): {e}")
            await ctx.send(f"Could not move to your channel: {e}")
            return None
    else:
        logger.warning(f"Bot is busy in {ctx.voice_client.channel.name}, cannot move to {user_channel.name} for {ctx.author.name} (PID: {os.getpid()})")
        await ctx.send(f"I'm currently busy in **{ctx.voice_client.channel.name}**. Join me there, or wait until I'm free.")
        return None

# --- Core Music Playing Logic ---

def play_audio_source(ctx: commands.Context, source_url: str):
    guild_id = ctx.guild.id
    vc = ctx.voice_client
    logger.info(f"play_audio_source called for guild {guild_id} with URL (first ~50 chars): {source_url[:50]} (PID: {os.getpid()})")

    if vc and vc.is_connected():
        try:
            volume = guild_volumes.get(guild_id, 0.5) # Default volume 50%
            audio_source = discord.FFmpegPCMAudio(source_url, **FFMPEG_OPTIONS, executable=FFMPEG_PATH)
            transformed_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
            
            vc.play(transformed_source, after=lambda e: bot.loop.create_task(on_song_end(ctx, e)))
            logger.info(f"Started playing audio in guild {guild_id} (PID: {os.getpid()})")
        except Exception as e:
            logger.error(f"Error in play_audio_source for guild {guild_id} (PID: {os.getpid()}): {e}", exc_info=True)
            bot.loop.create_task(ctx.send(f"Error playing audio. See logs for details."))
            bot.loop.create_task(on_song_end(ctx, e)) # Attempt to cleanup or play next

async def on_song_end(ctx: commands.Context, error=None):
    guild_id = ctx.guild.id
    if error:
        logger.error(f"Player error in guild {guild_id} (PID: {os.getpid()}): {error}", exc_info=True)

    current_song_info.pop(guild_id, None) # Clear current song for this guild
    logger.info(f"Song ended or skipped in guild {guild_id}. Checking queue. (PID: {os.getpid()})")
    
    if music_queues.get(guild_id): # Check if there are more songs in the queue
        await play_next_in_queue(ctx)
    else:
        logger.info(f"Queue empty for guild {guild_id} after song end. (PID: {os.getpid()})")
        pass


async def play_next_in_queue(ctx: commands.Context):
    guild_id = ctx.guild.id
    logger.debug(f"play_next_in_queue called for guild {guild_id} (PID: {os.getpid()})")
    if music_queues.get(guild_id):
        song = music_queues[guild_id].popleft()
        current_song_info[guild_id] = song
        
        logger.info(f"Playing next in queue for guild {guild_id}: '{song['title']}' requested by {song['requester'].name} (PID: {os.getpid()})")
        await ctx.send(f"Now playing: **{song['title']}** (requested by {song['requester'].mention})")
        play_audio_source(ctx, song['source_url'])
    else:
        logger.info(f"play_next_in_queue called for guild {guild_id}, but queue is now empty. (PID: {os.getpid()})")


# --- Bot Events ---
@bot.event
async def on_ready():
    logger.info(f"Bot '{bot.user.name}' (ID: {bot.user.id}) has connected to Discord!")
    logger.info(f"Operating with PID: {os.getpid()}") # Crucial for diagnosing multiple instances
    logger.info(f"Command prefix: {bot.command_prefix}")
    if discord.opus.is_loaded():
        logger.info("Opus library (used with PyNaCl for voice) is loaded.")
    else:
        logger.warning("Opus library (used with PyNaCl for voice) is NOT loaded. Voice might not work. "
                       "Ensure libopus is installed on your system if voice issues occur.")
    logger.info('Ready to play music!')

# --- Bot Command Hook for Logging ---
# This hook runs before every command.
async def before_invoke_hook(ctx: commands.Context):
    logger.info(
        f"CMD EXEC: '{ctx.command.qualified_name}' by {ctx.author} (ID: {ctx.author.id}) "
        f"in Guild: {ctx.guild.name} (ID: {ctx.guild.id}). "
        f"Full msg: '{ctx.message.content}'. PID: {os.getpid()}"
    )
bot.before_invoke(before_invoke_hook)


# --- Bot Commands ---
@bot.command(name='join', aliases=['connect'], help='Joins your current voice channel.')
async def join(ctx: commands.Context):
    # ensure_voice and the before_invoke_hook will log sufficiently
    if ctx.author.voice:
        user_channel = ctx.author.voice.channel
        if ctx.voice_client is None:
            try:
                await user_channel.connect()
                await ctx.send(f"Joined **{user_channel.name}**.")
            except Exception as e:
                await ctx.send(f"Could not join your channel: {e}")
        elif ctx.voice_client.channel == user_channel:
            await ctx.send("Already in your voice channel.")
        else: # Bot in another channel in this guild, try to move
            try:
                await ctx.voice_client.move_to(user_channel)
                await ctx.send(f"Moved to **{user_channel.name}**.")
            except Exception as e:
                 await ctx.send(f"Could not move to your channel. I might be playing music or an error occurred: {e}")
    else:
        await ctx.send("You are not in a voice channel. Join one first!")

@bot.command(name='leave', aliases=['disconnect', 'dc'], help='Leaves the voice channel and clears the queue.')
async def leave(ctx: commands.Context):
    guild_id = ctx.guild.id
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        music_queues.pop(guild_id, None)
        current_song_info.pop(guild_id, None)
        await ctx.send("Disconnected from the voice channel and cleared queue.")
    else:
        await ctx.send("I'm not in a voice channel.")

@bot.command(name='play', aliases=['p'], help='Plays a song from YouTube (URL or search query).')
async def play(ctx: commands.Context, *, query: str):
    guild_id = ctx.guild.id
    
    vc = await ensure_voice(ctx)
    if not vc: # ensure_voice sends its own messages if it fails
        return

    if guild_id not in music_queues:
        music_queues[guild_id] = deque()

    # Send a "searching" message immediately for better UX
    # You can use a custom loading emoji if your bot has access to one.
    # loading_emoji = discord.utils.get(bot.emojis, name="discordloading") or "⏳"
    search_message = await ctx.send(f"Searching for: `{query}` ⏳")

    async with ctx.typing(): # Shows "Bot is typing..." for the yt-dlp part
        loop = asyncio.get_event_loop()
        
        def extract_yt_info_sync(search_query_or_url, ydl_opts_sync):
            logger.debug(f"yt-dlp: Starting extraction for '{search_query_or_url}' (PID: {os.getpid()})")
            with yt_dlp.YoutubeDL(ydl_opts_sync) as ydl:
                try:
                    info = ydl.extract_info(search_query_or_url, download=False)
                    # logger.debug(f"yt-dlp: Extraction successful for '{search_query_or_url}'. Info keys: {list(info.keys()) if info else 'None'}")
                    return info
                except yt_dlp.utils.DownloadError as de:
                    # This specifically catches issues like "video unavailable" or region locks during info extraction
                    logger.warning(f"yt-dlp DownloadError for '{search_query_or_url}' (PID: {os.getpid()}): {str(de).splitlines()[0]}") # Log first line
                    return {"_type": "error", "error_msg": str(de)}
                except Exception as e_sync:
                    logger.error(f"yt-dlp: Unexpected error during sync extraction for '{search_query_or_url}' (PID: {os.getpid()}): {e_sync}", exc_info=True)
                    return {"_type": "error", "error_msg": "An unexpected error occurred during video information retrieval."}

        try:
            raw_info = await loop.run_in_executor(None, extract_yt_info_sync, query, YDL_OPTIONS)

            if not raw_info or raw_info.get("_type") == "error":
                error_msg = raw_info.get("error_msg", "Could not fetch song information.") if raw_info else "Could not fetch song information."
                logger.warning(f"Play cmd: yt-dlp failed for query '{query}' in guild {guild_id}. Message: {error_msg} (PID: {os.getpid()}).")
                await search_message.edit(content=f"Could not get information for `{query}`. It might be unavailable, private, or a search yielded no results.")
                return

            entry = None
            # yt-dlp with 'ytsearch1:' should directly give the first result, not a list of entries.
            # However, if a direct URL to a playlist is given and 'noplaylist' is True in YDL_OPTIONS,
            # 'entries' might still appear with one item.
            if 'entries' in raw_info and raw_info['entries']:
                # This typically means a playlist URL was given, and we take the first item due to 'noplaylist': True.
                # Or if 'ytsearchN:' (N > 1) was used, but we use 'ytsearch1:'.
                entry = raw_info['entries'][0]
                logger.info(f"Play cmd: yt-dlp returned a list of entries for '{query}', using first one: '{entry.get('title', 'N/A')}' (PID: {os.getpid()})")
            elif 'url' in raw_info: # Expected for single video (from search or direct URL)
                entry = raw_info
                logger.info(f"Play cmd: yt-dlp found a single entry for '{query}': '{entry.get('title', 'N/A')}' (PID: {os.getpid()})")
            else:
                logger.warning(f"Play cmd: yt-dlp returned unexpected structure for query '{query}' in guild {guild_id}. (PID: {os.getpid()}). Keys: {list(raw_info.keys()) if isinstance(raw_info, dict) else 'Not a dict'}")
                await search_message.edit(content=f"Could not find a playable track from your query: `{query}`.")
                return
            
            stream_url = entry.get('url') # This should be the direct audio stream URL
            if not stream_url:
                logger.error(f"Play cmd: No stream_url in yt-dlp entry for '{entry.get('title', query)}' despite extraction. (PID: {os.getpid()}). Entry keys: {list(entry.keys())}")
                await search_message.edit(content="Found video information, but couldn't get a playable audio stream. The format might be unsupported.")
                return

            song_details = {
                'webpage_url': entry.get('webpage_url', "N/A"),
                'title': entry.get('title', 'Unknown Title'),
                'duration': entry.get('duration'),
                'uploader': entry.get('uploader', 'Unknown Uploader'),
                'thumbnail': entry.get('thumbnail'),
                'requester': ctx.author,
                'source_url': stream_url
            }

            music_queues[guild_id].append(song_details)
            await search_message.edit(content=f"Added to queue: **{song_details['title']}**")
            logger.info(f"Play cmd: Added '{song_details['title']}' to queue in guild {guild_id} (PID: {os.getpid()})")

        except Exception as e:
            logger.error(f"Play cmd: Unexpected error for query '{query}' in guild {guild_id} (PID: {os.getpid()}): {e}", exc_info=True)
            await search_message.edit(content=f"An unexpected error occurred while trying to process your request.")
            return

    if not vc.is_playing() and not vc.is_paused():
        logger.info(f"Play cmd: VC not playing in guild {guild_id}, starting playback. (PID: {os.getpid()})")
        await play_next_in_queue(ctx)
    else:
        logger.info(f"Play cmd: VC already playing/paused in guild {guild_id}, song queued. (PID: {os.getpid()})")

# ... (skip, stop, pause, resume, queue, nowplaying, volume commands remain largely the same, but would benefit from PID in their logs too if debugging extensively)
# For brevity, I'll skip adding PID to every single log line in those, but the pattern is established.

@bot.command(name='skip', aliases=['s'], help='Skips the current song.')
async def skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("Skipped current song.")
    else:
        await ctx.send("Not playing anything or queue is empty, nothing to skip.")

@bot.command(name='stop', help='Stops playback, clears the queue, and leaves the channel.')
async def stop(ctx: commands.Context):
    guild_id = ctx.guild.id
    vc = ctx.voice_client
    if vc:
        music_queues.pop(guild_id, None)
        current_song_info.pop(guild_id, None)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()
        await ctx.send("Playback stopped, queue cleared, and disconnected.")
    else:
        await ctx.send("I'm not connected to a voice channel.")


@bot.command(name='pause', help='Pauses the current song.')
async def pause(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("Paused playback. Use `m!resume` to continue.")
    else:
        await ctx.send("Not playing anything or already paused.")

@bot.command(name='resume', aliases=['r', 'unpause'], help='Resumes the current song.')
async def resume(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("Resumed playback.")
    else:
        await ctx.send("Not paused or nothing to resume.")


@bot.command(name='queue', aliases=['q', 'playlist'], help='Shows the current song queue.')
async def queue_cmd(ctx: commands.Context):
    guild_id = ctx.guild.id
    queue = music_queues.get(guild_id)
    embed = discord.Embed(title="Music Queue", color=discord.Color.blue())
    
    song_now = current_song_info.get(guild_id)
    if song_now and ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        duration_str = str(datetime.timedelta(seconds=int(song_now.get('duration', 0)))) if song_now.get('duration') else "N/A"
        embed.add_field(
            name="Now Playing", 
            value=f"[{song_now['title']}]({song_now['webpage_url']}) | `{duration_str}` | Req by: {song_now['requester'].mention}", 
            inline=False
        )
        if song_now.get('thumbnail'):
            embed.set_thumbnail(url=song_now['thumbnail'])
    
    if not queue:
        if not song_now: # No current song and no queue
            await ctx.send("The queue is currently empty.")
            return
        else: # Only current song is playing
            await ctx.send(embed=embed)
            return

    queue_list_str = ""
    for i, song_item in enumerate(list(queue)[:10]):
        duration_str = str(datetime.timedelta(seconds=int(song_item.get('duration', 0)))) if song_item.get('duration') else "N/A"
        queue_list_str += f"{i+1}. [{song_item['title']}]({song_item['webpage_url']}) | `{duration_str}` | Req by: {song_item['requester'].mention}\n"
    
    if queue_list_str: # Add "Up Next" field only if there are songs in the string
        embed.add_field(name="Up Next", value=queue_list_str, inline=False)

    if len(queue) > 10:
        embed.set_footer(text=f"...and {len(queue) - 10} more song(s).")
    elif not queue_list_str and not song_now: # Should be caught by the first check
         await ctx.send("The queue is currently empty.")
         return
        
    await ctx.send(embed=embed)


@bot.command(name='nowplaying', aliases=['np', 'current'], help='Shows the currently playing song.')
async def nowplaying_cmd(ctx: commands.Context):
    guild_id = ctx.guild.id
    song = current_song_info.get(guild_id)
    vc = ctx.voice_client

    if song and vc and (vc.is_playing() or vc.is_paused()):
        embed = discord.Embed(title="Now Playing", color=discord.Color.green())
        embed.add_field(name="Title", value=f"[{song['title']}]({song['webpage_url']})", inline=False)
        embed.add_field(name="Requested by", value=song['requester'].mention, inline=True)
        
        if song.get('duration'):
            duration_str = str(datetime.timedelta(seconds=int(song['duration'])))
            embed.add_field(name="Duration", value=duration_str, inline=True)
        if song.get('uploader'):
            embed.add_field(name="Uploader", value=song['uploader'], inline=True)
        if song.get('thumbnail'):
            embed.set_thumbnail(url=song['thumbnail'])
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("Not currently playing any song.")

@bot.command(name='volume', aliases=['vol'], help='Changes player volume (0-200). Default: 50. Usage: m!volume <number>')
async def volume(ctx: commands.Context, new_volume: int):
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await ctx.send("Not playing anything right now.")

    if not 0 <= new_volume <= 200:
        return await ctx.send("Volume must be between 0 and 200.")

    actual_volume = new_volume / 100.0
    guild_volumes[guild_id] = actual_volume

    if vc.source and hasattr(vc.source, 'volume'):
        vc.source.volume = actual_volume
        await ctx.send(f"Volume set to {new_volume}%.")
    else:
        await ctx.send(f"Volume will be set to {new_volume}% for the next song (could not adjust current source).")

# --- Error Handling for commands ---
@bot.event
async def on__command_error(ctx: commands.Context, error):
    # Log the error with more context
    logger.error(
        f"Error in command '{ctx.command.qualified_name if ctx.command else 'UnknownCommand'}'. "
        f"Invoked by: {ctx.author}. Message: '{ctx.message.content}'. "
        f"PID: {os.getpid()}. Error: {type(error).__name__}: {error}",
        exc_info=True if not isinstance(error, (commands.CommandNotFound, commands.MissingRequiredArgument, commands.BadArgument)) else False
    )

    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"Invalid command. Use `{bot.command_prefix}help` to see available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Use `{bot.command_prefix}help {ctx.command.name}` for details.")
    elif isinstance(error, commands.CommandInvokeError):
        original_error = error.original
        if isinstance(original_error, discord.errors.ClientException) and "Already connected" in str(original_error):
             await ctx.send("There was an issue with voice connection management. Try again or use `m!leave` and `m!join`.")
        elif isinstance(original_error, yt_dlp.utils.DownloadError): # This might be less frequent if extract_yt_info_sync catches it
            await ctx.send("Error downloading or processing song information. The video might be unavailable or restricted.")
        elif "PyNaCl is not installed" in str(original_error) or "opus is not loaded" in str(original_error).lower():
            await ctx.send("Voice components (PyNaCl/Opus) are missing or not loaded correctly. Please ensure PyNaCl is installed and libopus is available on your system.")
        else:
            await ctx.send(f"An error occurred executing that command. Please check the logs or try again.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Invalid argument provided. Check `{bot.command_prefix}help {ctx.command.name}`.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("You do not have permissions to use this command.")
    else:
        await ctx.send("An unexpected error occurred. The developers have been notified (check logs).")

# --- Main execution point ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("Bot token not found in environment. Exiting.")
        exit()
    else:
        try:
            logger.info(f"Attempting to start the bot with PID: {os.getpid()}...")
            # When using custom logging setup, pass log_handler=None to bot.run
            # to prevent discord.py from configuring the root logger.
            bot.run(BOT_TOKEN, log_handler=None)
        except discord.errors.LoginFailure:
            logger.critical("Failed to log in. Check if your bot token is correct and valid.", exc_info=True)
        except ImportError as e: # Should be caught by earlier explicit nacl import
            logger.critical(f"A critical import error occurred: {e}. Make sure all dependencies are installed.", exc_info=True)
        except Exception as e:
            logger.critical(f"A critical error occurred during bot startup or runtime: {e}", exc_info=True)
