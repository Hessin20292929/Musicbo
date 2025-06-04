import discord
from discord.ext import commands, tasks
import asyncio
import yt_dlp
import os
from collections import deque
import datetime # For formatting duration in nowplaying

# --- Bot Configuration ---
# IMPORTANT: Set your bot token as an environment variable for security.
# On Linux/macOS: export DISCORD_MUSIC_BOT_TOKEN="YOUR_TOKEN_HERE"
# On Windows (PowerShell): $env:DISCORD_MUSIC_BOT_TOKEN="YOUR_TOKEN_HERE"
# Or, for testing ONLY, you can uncomment and set it here:
# BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
BOT_TOKEN = os.environ.get("DISCORD_MUSIC_BOT_TOKEN")

if not BOT_TOKEN:
    print("CRITICAL: Bot token not found. Please set the DISCORD_MUSIC_BOT_TOKEN environment variable.")
    exit()

# Path to FFmpeg executable. If it's in your system PATH, 'ffmpeg' is usually fine.
# Otherwise, provide the full path, e.g., "C:/ffmpeg/bin/ffmpeg.exe"
FFMPEG_PATH = "ffmpeg" 

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'  # No video, audio only
}

YDL_OPTIONS = {
    'format': 'bestaudio/best', # Choose best audio format
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s', # Output template
    'restrictfilenames': True,
    'noplaylist': True,        # Download only single video if not a playlist URL
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch', # Default search to YouTube
    'source_address': '0.0.0.0'  # Fix for some IPv6 issues
}

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content for commands
intents.voice_states = True     # Required for voice channel operations

bot = commands.Bot(command_prefix="m!", intents=intents) # Change "m!" to your desired prefix

# --- Per-Guild State Management ---
# These dictionaries will store data for each server (guild) the bot is in.
music_queues = {}  # guild_id: deque of song_info dictionaries
current_song_info = {}  # guild_id: song_info dictionary for the currently playing song
guild_volumes = {} # guild_id: float (volume level, 0.0 to 2.0)


# --- Helper Functions ---

async def ensure_voice(ctx: commands.Context):
    """Checks if the user is in a voice channel and connects/moves the bot if necessary."""
    if not ctx.author.voice:
        await ctx.send("You are not connected to a voice channel.")
        return None

    user_channel = ctx.author.voice.channel
    if not ctx.voice_client: # Bot is not connected to any voice channel in this guild
        try:
            vc = await user_channel.connect()
            return vc
        except discord.ClientException as e:
            await ctx.send(f"Error connecting to voice channel: {e}")
            return None
    
    # Bot is connected, check if it's in the same channel as the user
    if ctx.voice_client.channel == user_channel:
        return ctx.voice_client # Already in the correct channel
    
    # Bot is in a different channel
    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
        try:
            await ctx.voice_client.move_to(user_channel)
            await ctx.send(f"Moved to **{user_channel.name}**.")
            return ctx.voice_client
        except Exception as e:
            await ctx.send(f"Could not move to your channel: {e}")
            return None
    else:
        await ctx.send(f"I'm currently busy in **{ctx.voice_client.channel.name}**. Join me there, or wait until I'm free.")
        return None

# --- Core Music Playing Logic ---

def play_audio_source(ctx: commands.Context, source_url: str):
    """Plays the audio from source_url in the guild's voice client."""
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    if vc and vc.is_connected():
        try:
            volume = guild_volumes.get(guild_id, 0.5) # Default volume 50%
            audio_source = discord.FFmpegPCMAudio(source_url, **FFMPEG_OPTIONS, executable=FFMPEG_PATH)
            transformed_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
            
            vc.play(transformed_source, after=lambda e: bot.loop.create_task(on_song_end(ctx, e)))
        except Exception as e:
            bot.loop.create_task(ctx.send(f"Error playing audio: {e}"))
            print(f"Error in play_audio_source for guild {guild_id}: {e}")
            bot.loop.create_task(on_song_end(ctx, e)) # Attempt to cleanup or play next

async def on_song_end(ctx: commands.Context, error=None):
    """Callback function for when a song finishes playing or an error occurs."""
    guild_id = ctx.guild.id
    if error:
        print(f"Player error in guild {guild_id}: {error}")
        # Avoid sending too many error messages if it's a rapid succession of errors
        # await ctx.send(f"Playback error: {error}. Trying next song if available.")

    current_song_info.pop(guild_id, None) # Clear current song for this guild
    
    if music_queues.get(guild_id): # Check if there are more songs in the queue
        await play_next_in_queue(ctx)
    else:
        # Optional: Add an inactivity disconnect timer here
        # For now, it stays in the channel. A message can be sent if desired.
        # await ctx.send("Queue finished.") 
        pass


async def play_next_in_queue(ctx: commands.Context):
    """Plays the next song in the guild's queue."""
    guild_id = ctx.guild.id
    if music_queues.get(guild_id):
        song = music_queues[guild_id].popleft()
        current_song_info[guild_id] = song
        
        await ctx.send(f"Now playing: **{song['title']}** (requested by {song['requester'].mention})")
        play_audio_source(ctx, song['source_url'])
    # If queue becomes empty after pop, on_song_end will handle it.

# --- Bot Events ---

@bot.event
async def on_ready():
    print(f'{bot.user.name} is now online!')
    print(f'Bot ID: {bot.user.id}')
    print(f'Command prefix: {bot.command_prefix}')
    print('Ready to play music!')

# --- Bot Commands ---

@bot.command(name='join', aliases=['connect'], help='Joins your current voice channel.')
async def join(ctx: commands.Context):
    if ctx.author.voice:
        user_channel = ctx.author.voice.channel
        if ctx.voice_client is None: # Bot not connected in this guild
            try:
                await user_channel.connect()
                await ctx.send(f"Joined **{user_channel.name}**.")
            except Exception as e:
                await ctx.send(f"Could not join your channel: {e}")
        elif ctx.voice_client.channel == user_channel: # Bot already in user's channel
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
        # guild_volumes.pop(guild_id, None) # Optionally reset volume setting on leave
        await ctx.send("Disconnected from the voice channel and cleared queue.")
    else:
        await ctx.send("I'm not in a voice channel.")

@bot.command(name='play', aliases=['p'], help='Plays a song from YouTube (URL or search). Usage: m!play <song name or URL>')
async def play(ctx: commands.Context, *, query: str):
    guild_id = ctx.guild.id
    
    vc = await ensure_voice(ctx)
    if not vc: # ensure_voice sends its own messages if it fails
        return

    if guild_id not in music_queues:
        music_queues[guild_id] = deque()

    async with ctx.typing(): # Shows "Bot is typing..."
        loop = asyncio.get_event_loop()
        
        def extract_yt_info_sync(search_query_or_url, ydl_opts):
            """Synchronous wrapper for yt-dlp extraction."""
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(search_query_or_url, download=False)

        try:
            # Run blocking yt-dlp in a separate thread
            raw_info = await loop.run_in_executor(None, extract_yt_info_sync, query, YDL_OPTIONS)

            if not raw_info:
                await ctx.send("Could not fetch song information. Try again.")
                return

            if 'entries' in raw_info: # Search result list or playlist (noplaylist=True means first item of playlist)
                if not raw_info['entries']:
                    await ctx.send(f"No search results found for '{query}'. Try different keywords or a direct URL.")
                    return
                entry = raw_info['entries'][0] # Take the first result
            else: # Single video
                entry = raw_info
            
            if not entry:
                await ctx.send("Could not find a playable track from your query.")
                return

            stream_url = entry.get('url') # This is usually the direct stream if 'format' is good
            
            # Fallback if 'url' is not at the top level (rare for 'bestaudio/best' with single item)
            if not stream_url and entry.get('webpage_url'):
                single_ydl_opts = YDL_OPTIONS.copy()
                single_ydl_opts['noplaylist'] = True # ensure it's a single video for re-extraction
                single_video_info = await loop.run_in_executor(None, extract_yt_info_sync, entry['webpage_url'], single_ydl_opts)
                stream_url = single_video_info.get('url')
                if not stream_url: # Try formats if still not found (more robust fallback)
                    audio_formats = [f for f in single_video_info.get('formats', []) if f.get('acodec') != 'none' and 'url' in f]
                    if audio_formats:
                        audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True) # Best audio bitrate
                        stream_url = audio_formats[0]['url']
            
            if not stream_url:
                await ctx.send("Could not find a suitable audio stream for this video. It might be region-locked or private.")
                return

            song_details = {
                'webpage_url': entry.get('webpage_url', query),
                'title': entry.get('title', 'Unknown Title'),
                'duration': entry.get('duration'),
                'uploader': entry.get('uploader', 'Unknown Uploader'),
                'thumbnail': entry.get('thumbnail'),
                'requester': ctx.author,
                'source_url': stream_url
            }

            music_queues[guild_id].append(song_details)
            await ctx.send(f"Added to queue: **{song_details['title']}**")

        except yt_dlp.utils.DownloadError as e:
            await ctx.send(f"Error fetching song: The video might be unavailable, private, or region-restricted.")
            print(f"yt-dlp DownloadError: {e}")
            return
        except Exception as e:
            await ctx.send(f"An unexpected error occurred while trying to play the song.")
            print(f"Error in play command (guild {guild_id}): {e}")
            return

    if not vc.is_playing() and not vc.is_paused():
        await play_next_in_queue(ctx)

@bot.command(name='skip', aliases=['s'], help='Skips the current song.')
async def skip(ctx: commands.Context):
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop() # This triggers the 'after' callback in vc.play(), which calls on_song_end
        await ctx.send("Skipped current song.")
        # on_song_end will then try to play the next song if available.
    else:
        await ctx.send("Not playing anything or queue is empty, nothing to skip.")

@bot.command(name='stop', help='Stops playback, clears the queue, and leaves the channel.')
async def stop(ctx: commands.Context):
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    if vc:
        music_queues.pop(guild_id, None) # Clear queue
        current_song_info.pop(guild_id, None) # Clear current song
        if vc.is_playing() or vc.is_paused():
            vc.stop() # Stop playback
        await vc.disconnect() # Disconnect
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
        if not song_now:
            await ctx.send("The queue is currently empty.")
            return
        else: # Only current song is playing, queue is empty
            await ctx.send(embed=embed)
            return

    queue_list_str = ""
    for i, song in enumerate(list(queue)[:10]): # Display up to 10 songs in queue
        duration_str = str(datetime.timedelta(seconds=int(song.get('duration', 0)))) if song.get('duration') else "N/A"
        queue_list_str += f"{i+1}. [{song['title']}]({song['webpage_url']}) | `{duration_str}` | Req by: {song['requester'].mention}\n"
    
    if queue_list_str:
        embed.add_field(name="Up Next", value=queue_list_str, inline=False)

    if len(queue) > 10:
        embed.set_footer(text=f"...and {len(queue) - 10} more song(s).")
    elif not queue_list_str and not song_now: # Should be caught earlier
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

    if not 0 <= new_volume <= 200: # Allow up to 200% volume
        return await ctx.send("Volume must be between 0 and 200.")

    # PCMVolumeTransformer takes volume from 0.0 to 2.0
    actual_volume = new_volume / 100.0
    guild_volumes[guild_id] = actual_volume # Save for future songs in this guild

    if hasattr(vc.source, 'volume'):
        vc.source.volume = actual_volume
        await ctx.send(f"Volume set to {new_volume}%.")
    else:
        await ctx.send(f"Volume will be set to {new_volume}% for the next song (could not adjust current).")


# --- Error Handling for commands ---
@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"Invalid command. Use `{bot.command_prefix}help` to see available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Use `{bot.command_prefix}help {ctx.command.name}` for details.")
    elif isinstance(error, commands.CommandInvokeError):
        original_error = error.original
        print(f"CommandInvokeError in '{ctx.command}': {original_error}") # Log the full original error
        if isinstance(original_error, discord.errors.ClientException) and "Already connected" in str(original_error):
             await ctx.send("There was an issue with voice connection management. Try again or use `m!leave` and `m!join`.")
        elif isinstance(original_error, yt_dlp.utils.DownloadError):
            await ctx.send("Error downloading or processing song information. The video might be unavailable or restricted.")
        else:
            await ctx.send(f"An error occurred executing that command. Please try again or check logs.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Invalid argument provided. Check `{bot.command_prefix}help {ctx.command.name}`.")
    else:
        print(f"Unhandled error in '{ctx.command}': {error}") # Log other errors
        await ctx.send("An unexpected error occurred.")


# --- Main execution point ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("CRITICAL: Bot token (DISCORD_MUSIC_BOT_TOKEN) is not set in environment variables.")
        print("The bot cannot start without a token.")
    else:
        try:
            print("Starting bot...")
            bot.run(BOT_TOKEN)
        except discord.errors.LoginFailure:
            print("CRITICAL: Failed to log in. Check if your bot token is correct and valid.")
        except Exception as e:
            print(f"CRITICAL: An error occurred during bot startup or runtime: {e}")
