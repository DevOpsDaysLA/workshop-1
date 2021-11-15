import glob
import json
import os
import sys
import uuid

import validators
from errbot import BotPlugin, arg_botcmd, botcmd
from lib.chat.discord import Discord
from lib.chat.discord_custom import DiscordCustom
from lib.common.cooldown import CoolDown
from lib.common.utilities import Util
from lib.common.youtube_dl_lib import YtdlLib
from lib.database.dynamo_tables import PlayTable

# cooldown = CoolDown(10, PlayTable) # uncomment to enable cooldowl
util = Util()
discord = Discord()
ytdl = YtdlLib()

QUEUE_PATH = "plugins/play/queue"

QUEUE_ERROR_MSG = f"❌ An error occuring writing your request to the `.play` queue!"


class Play(BotPlugin):
    """Play plugin for Errbot"""

    def activate(self):
        """
        Runs the play_cron() function every interval
        Note: the self.start_polling() function will wait for the first cron job to finish before starting the next one
        """
        super().activate()
        disabled = os.environ.get("DISABLE_PLAY_CRON", False)
        if disabled:
            print("Play cron disabled for local testing")
            sys.stdout.flush()
        else:
            interval = 5
            self.start_poller(interval, self.play_cron)

    def play_cron(self):
        """
        The core logic for the .play cron (aka running from the queue)
        """

        print('play cron has started')
        sys.stdout.flush()

        # Scans all the .play queue files (checks all guilds/servers)
        for queue in self.scan_queue_dir():
            # If a queue file is found for a guild/server, read it
            queue_items = self.read_queue(queue)

            # If the queue is empty, return
            if len(queue_items) == 0:
                return

            # Load the first item in the queue since we are processing songs in FIFO order
            queue_item = queue_items[0]

            # Play the item in the queue
            out_file = ytdl.download_audio(queue_item['url'])
            dc = DiscordCustom(self._bot)
            dc.play_audio_file(queue_item['discord_channel_id'], out_file)

            # Remove the item from the queue after it has been played
            self.delete_from_queue(queue_item['guild_id'], queue_item['song_uuid'])

    @arg_botcmd("--url", dest="url", type=str, default=None)
    @arg_botcmd("--channel", dest="channel", type=int, default=None)
    def play(self, msg, url=None, channel=None):
        """
        Play a YouTube video in chat!

        --channel <channel ID> - The full channel id to play the video/audio in
        --url <url> - The full url of the video/audio to play in chat
        """

        # Run some validation on the URL the user is providing
        if not validators.url(url):
            return "❌ Invalid URL"
        if not url.startswith("https://www.youtube.com/"):
            return "❌ I only accept URLs that start with `https://www.youtube.com/`"

        # Check the user's cooldown for the .play command
        # allowed = cooldown.check(msg) # uncomment to enable cooldowl
        allowed = True # comment to enable cooldowl

        if allowed:
            # Get all the metadata for a given video from a URL
            video_metadata = ytdl.video_metadata(url)

            length = video_metadata['duration']
            # If the length is 0 it is probably a live stream
            if length == 0:
                return f"❌ Cannot play a live stream from YouTube"

            # If the video is greater than the configured max length, don't play it
            if length > ytdl.max_length:
                return f"❌ Video is longer than the max accepted length: `{ytdl.max_length}` seconds"

            # Check if the queue .json file is read for reads/writes
            file_ready = util.check_file_ready(f"{QUEUE_PATH}/{discord.guild_id(msg)}_queue.json")

            # If it is not ready and open by another process we have to exit
            if not file_ready:
                return QUEUE_ERROR_MSG

            # If the queue file is ready, we can add the song to the queue
            result = self.add_to_queue(msg, channel, video_metadata)

            # If something went wrong, we can't add the song to the queue and send an error message
            if not result:
                return QUEUE_ERROR_MSG

            # If we got this far, the song has been queue'd and will be picked up and played by the cron
            return f"🎵 Your song is queued up: `{video_metadata['title']}`"

        else:
            # A user is trying to play a song too quickly
            message = "Slow down!\n"
            # message += f"⏲️ Cooldown expires in `{cooldown.remaining()}`" # uncomment to enable cooldowl
            return message

    @botcmd
    def play_queue(self, msg, args):
        """
        See what is in the .play queue
        Usage: .play queue
        """
        queue_items = self.read_queue(discord.guild_id(msg))

        # If the queue is empty, return
        if len(queue_items) == 0:
            return "🎵 No songs in the queue"

        # If the queue is not empty, return the queue items
        message = "🎵 Songs in the queue:\n"
        for place, item in enumerate(queue_items):
            message += f"{place + 1}: `{item['song']}`\n"

        return message

    def read_queue(self, guild_id):
        """
        Helper function - Read the .play queue for a given guild/server
        :param guild_id: The guild/server ID
        :return: A list of queue items - each item is a dictionary
        """
        try:
            with open(f"{QUEUE_PATH}/{guild_id}_queue.json", "r") as queue_file:
                queue_items = json.loads(queue_file.read())
            return queue_items
        except FileNotFoundError:
            return []

    def scan_queue_dir(self):
        """
        Helper function for scanning all the .play queue files
        :return: A list of all the queue files (each item in the list is an int of the guild ID)
        """
        queue_files = []
        for filepath in glob.iglob(f'{QUEUE_PATH}/*.json'):
            queue_files.append(int(filepath.strip().replace("_queue.json", "").replace(f"{QUEUE_PATH}/", "")))
        return queue_files

    def add_to_queue(self, msg, channel, video_metadata):
        """
        Helper function - Add a song to the .play queue
        """

        queue_path = f"{QUEUE_PATH}/{discord.guild_id(msg)}_queue.json"

        queue_item = {
            "guild_id": discord.guild_id(msg),
            "user_id": discord.get_user_id(msg),
            "discord_channel_id": channel,
            "song_uuid": str(uuid.uuid4()),
            "song": video_metadata['title'],
            "song_duration": video_metadata['duration'],
            "url": video_metadata['webpage_url'],
        }

        # Check if the queue file exists
        file_exists = os.path.exists(queue_path)

        # The the file exists, we read the queue data
        if file_exists:
            with open(queue_path, "r+") as queue_file:
                # Append to the queue with the new queue item
                queue = json.loads(queue_file.read()) + [queue_item]
                # Seek to the start of the file and nuke the contents
                queue_file.seek(0)
                queue_file.truncate(0)
                # Overwrite the file with the new queue data
                queue_file.write(json.dumps(queue))
        # If the file doesn't exist, we create it and add the queue item
        else:
            with open(queue_path, "w") as queue_file:
                queue_file.write(json.dumps([queue_item]))

        return True

    def delete_from_queue(self, guild_id, song_uuid):
        """
        Helper function - Delete a song from the .play queue
        :param guild_id: The guild/server ID of the queue file
        :param song_uuid: The song UUID to delete
        """
        # Compute the queue path
        queue_path = f"{QUEUE_PATH}/{guild_id}_queue.json"
        # Check if the queue file exists
        file_exists = os.path.exists(queue_path)

        # If the file exists, we read the queue data
        if file_exists:
            with open(queue_path, "r+") as queue_file:
                # Read and load the queue data as json
                queue = json.loads(queue_file.read())

                # Loop through the queue and delete the item with the matching UUID
                queue_list = []
                for item in queue:
                    # If the UUID matches, we skip it (effectively deleting it)
                    if item["song_uuid"] == song_uuid:
                        continue
                    # Add all the other non-matching items to the queue list
                    else:
                        queue_list.append(item)

                # Seek to the start of the file and nuke the contents
                queue_file.seek(0)
                queue_file.truncate(0)
                # Overwrite the file with the new queue data
                queue_file.write(json.dumps(queue_list))
