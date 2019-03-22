#!/usr/bin/env python

from __future__ import print_function

import sys
import re
from enum import Enum

import requests
import json
import time
import logging
import random
import string
import uuid
from threading import Thread
from config import Configuration
from database import DiscordChannel, AccountLinkToken, DiscordAccount
import database_session

from datetime import datetime, timedelta, timezone
import elasticsearch_logger as el
from minecraft import authentication
from minecraft.exceptions import YggdrasilError
from minecraft.networking.connection import Connection
from minecraft.networking.packets import clientbound, serverbound

import discord
import asyncio

from mcstatus import MinecraftServer

from bidict import bidict

log = logging.getLogger("bridge")

SESSION_TOKEN = ""
UUID_CACHE = bidict()
WEBHOOKS = []
BOT_USERNAME = ""
NEXT_MESSAGE_TIME = datetime.now(timezone.utc)
PREVIOUS_MESSAGE = ""
PLAYER_LIST = bidict()
PREVIOUS_PLAYER_LIST = bidict()
ACCEPT_JOIN_EVENTS = False
TAB_HEADER = ""
TAB_FOOTER = ""


def mc_uuid_to_username(uuid):
    if uuid not in UUID_CACHE:
        try:
            short_uuid = uuid.replace("-", "")
            mojang_response = requests.get("https://api.mojang.com/user/profiles/{}/names".format(short_uuid)).json()
            if len(mojang_response) > 1:
                # Multiple name changes
                player_username = mojang_response[-1]["name"]
            else:
                # Only one name
                player_username = mojang_response[0]["name"]
            UUID_CACHE[uuid] = player_username
            return player_username
        except Exception as e:
            log.error(e, exc_info=True)
            log.error("Failed to lookup {}'s username using the Mojang API.".format(uuid))
    else:
        return UUID_CACHE[uuid]

    
def mc_username_to_uuid(username):
    if username not in UUID_CACHE.inv:
        try:
            player_uuid = requests.get(
                "https://api.mojang.com/users/profiles/minecraft/{}".format(username)).json()["id"]
            long_uuid = uuid.UUID(player_uuid)
            UUID_CACHE.inv[username] = str(long_uuid)
            return player_uuid
        except:
            log.error("Failed to lookup {}'s UUID using the Mojang API.".format(username))
    else:
        return UUID_CACHE.inv[username]

        
def get_discord_help_string():
    help_str = ("Admin commands:\n"
                "`mc!chathere`: Starts outputting server messages in this channel\n"
                "`mc!stopchathere`: Stops outputting server messages in this channel\n"
                "User commands:\n"
                "`mc!tab`: Sends you the content of the server's player/tab list\n"
                "`mc!register`: Starts the minecraft account registration process\n"
                "To start chatting on the minecraft server, please register your account using `mc!register`.")
    return help_str


# https://stackoverflow.com/questions/33404752/removing-emojis-from-a-string-in-python
def remove_emoji(string):
    emoji_pattern = re.compile(
        "["
        u"\U0001F600-\U0001F64F"  # emoticons
        u"\U0001F300-\U0001F5FF"  # symbols & pictographs
        u"\U0001F680-\U0001F6FF"  # transport & map symbols
        u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
        u"\U0001F900-\U0001FAFF"  # CJK Compatibility Ideographs
        # u"\U00002702-\U000027B0"
        # u"\U000024C2-\U0001F251"
        "]+", flags=re.UNICODE)
    return emoji_pattern.sub(r'', string)


def escape_markdown(string):
    # Absolutely needs to go first or it will replace our escaping slashes!
    string = string.replace("\\", "\\\\")
    string = string.replace("_", "\\_")
    string = string.replace("*", "\\*")
    return string


def strip_colour(string):
    colour_pattern = re.compile(
        u"\U000000A7"  # selection symbol
        ".", flags=re.UNICODE)
    return colour_pattern.sub(r'', string)


def setup_logging(level):
    if level.lower() == "debug":
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    log_format = "%(asctime)s:%(name)s:%(levelname)s:%(message)s"
    logging.basicConfig(filename="bridge_log.log", format=log_format, level=log_level)
    stdout_logger=logging.StreamHandler(sys.stdout)
    stdout_logger.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(stdout_logger)


def run_auth_server(port):
    # We need to import twisted after setting up the logger because twisted hijacks our logging
    # TODO: Fix this in a cleaner way
    from twisted.internet import reactor
    from auth_server import AuthFactory

    # Create factory
    factory = AuthFactory()

    # Listen
    log.info("Starting authentication server on port {}".format(port))

    factory.listen("", port)
    try:
        reactor.run(installSignalHandlers=False)
    except KeyboardInterrupt:
        reactor.stop()


def generate_random_auth_token(length):
    letters = string.ascii_lowercase + string.digits + string.ascii_uppercase
    return ''.join(random.choice(letters) for i in range(length))


# TODO: Get rid of this when pycraft's enum becomes usable
class ChatType(Enum):
    CHAT = 0  # A player-initiated chat message.
    SYSTEM = 1  # The result of running a command.
    GAME_INFO = 2  # Displayed above the hotbar in vanilla clients.


def main():
    global BOT_USERNAME
    config = Configuration("config.json")
    setup_logging(config.logging_level)

    database_session.initialize(config)
    if config.es_enabled:
        el.initialize(config)

    reactor_thread = Thread(target=run_auth_server, args=(config.auth_port,))
    reactor_thread.start()

    def handle_disconnect():
        log.info('Disconnected.')
        global PLAYER_LIST, PREVIOUS_PLAYER_LIST, ACCEPT_JOIN_EVENTS
        PREVIOUS_PLAYER_LIST = PLAYER_LIST.copy()
        ACCEPT_JOIN_EVENTS = False
        PLAYER_LIST = bidict()
        if connection.connected:
            log.info("Forced a disconnection because the connection is still connected.")
            connection.disconnect(immediate=True)
        time.sleep(15)
        while not is_server_online():
            log.info('Not reconnecting to server because it appears to be offline.')
            time.sleep(15)
        log.info('Reconnecting.')
        connection.connect()

    def handle_disconnect_packet(join_game_packet):
        handle_disconnect()

    def minecraft_handle_exception(exception, exc_info):
        log.error("A minecraft exception occured! {}:".format(exception), exc_info=exc_info)
        handle_disconnect()

    def is_server_online():
        server = MinecraftServer.lookup("{}:{}".format(config.mc_server, config.mc_port))
        try:
            status = server.status()
            del status
            return True
        except ConnectionRefusedError:
            return False
        # AttributeError: 'TCPSocketConnection' object has no attribute 'socket'
        # This might not be required as it happens upstream
        except AttributeError:
            return False

    log.debug("Checking if the server {} is online before connecting.")

    if not config.mc_online:
        log.info("Connecting in offline mode...")
        while not is_server_online():
            log.info('Not connecting to server because it appears to be offline.')
            time.sleep(15)
        BOT_USERNAME = config.mc_username
        connection = Connection(
            config.mc_server, config.mc_port, username=config.mc_username,
            handle_exception=minecraft_handle_exception)
    else:
        auth_token = authentication.AuthenticationToken()
        try:
            auth_token.authenticate(config.mc_username, config.mc_password)
        except YggdrasilError as e:
            log.info(e)
            sys.exit()
        BOT_USERNAME = auth_token.profile.name
        log.info("Logged in as %s..." % auth_token.profile.name)
        while not is_server_online():
            log.info('Not connecting to server because it appears to be offline.')
            time.sleep(15)
        connection = Connection(
            config.mc_server, config.mc_port, auth_token=auth_token,
            handle_exception=minecraft_handle_exception)

    # Initialize the discord part
    discord_bot = discord.Client()

    def register_handlers(connection):
        connection.register_packet_listener(
            handle_join_game, clientbound.play.JoinGamePacket)

        connection.register_packet_listener(
            handle_chat, clientbound.play.ChatMessagePacket)

        connection.register_packet_listener(
            handle_health_update, clientbound.play.UpdateHealthPacket)

        connection.register_packet_listener(
            handle_disconnect_packet, clientbound.play.DisconnectPacket)

        connection.register_packet_listener(
            handle_tab_list, clientbound.play.PlayerListItemPacket)

        connection.register_packet_listener(
            handle_player_list_header_and_footer_update, clientbound.play.PlayerListHeaderAndFooterPacket)

    def handle_player_list_header_and_footer_update(header_footer_packet):
        global TAB_FOOTER, TAB_HEADER
        log.debug("Got Tablist H/F Update: header={}".format(header_footer_packet.header))
        log.debug("Got Tablist H/F Update: footer={}".format(header_footer_packet.footer))
        TAB_HEADER = json.loads(header_footer_packet.header)["text"]
        TAB_FOOTER = json.loads(header_footer_packet.footer)["text"]

    def handle_tab_list(tab_list_packet):
        global ACCEPT_JOIN_EVENTS
        log.debug("Processing tab list packet")
        for action in tab_list_packet.actions:
            if isinstance(action, clientbound.play.PlayerListItemPacket.AddPlayerAction):
                log.debug(
                    "Processing AddPlayerAction tab list packet, name: {}, uuid: {}".format(action.name, action.uuid))
                username = action.name
                player_uuid = action.uuid
                if action.name not in PLAYER_LIST.inv:
                    PLAYER_LIST.inv[action.name] = action.uuid
                else:
                    # Sometimes we get a duplicate add packet on join idk why
                    return
                if action.name not in UUID_CACHE.inv:
                    UUID_CACHE.inv[action.name] = action.uuid
                # Initial tablist backfill
                if ACCEPT_JOIN_EVENTS:
                    webhook_payload = {
                        'username': username,
                        'avatar_url':  "https://visage.surgeplay.com/face/160/{}".format(player_uuid),
                        'content': '',
                        'embeds': [{'color': 65280, 'title': '**Joined the game**'}]
                    }
                    for webhook in WEBHOOKS:
                        post = requests.post(webhook,json=webhook_payload)
                    if config.es_enabled:
                        el.log_connection(
                            uuid=action.uuid, reason=el.ConnectionReason.CONNECTED, count=len(PLAYER_LIST))
                    return
                else:
                    # The bot's name is sent last after the initial back-fill
                    if action.name == BOT_USERNAME:
                        ACCEPT_JOIN_EVENTS = True
                        if config.es_enabled:
                            diff = set(PREVIOUS_PLAYER_LIST.keys()) - set(PLAYER_LIST.keys())
                            for idx, uuid in enumerate(diff):
                                el.log_connection(uuid=uuid, reason=el.ConnectionReason.DISCONNECTED,
                                              count=len(PREVIOUS_PLAYER_LIST) - (idx + 1))
                        # Don't bother announcing the bot's own join message (who cares) but log it for analytics still
                        if config.es_enabled:
                            el.log_connection(
                                uuid=action.uuid, reason=el.ConnectionReason.CONNECTED, count=len(PLAYER_LIST))

                if config.es_enabled:
                    el.log_connection(uuid=action.uuid, reason=el.ConnectionReason.SEEN)
            if isinstance(action, clientbound.play.PlayerListItemPacket.RemovePlayerAction):
                log.debug("Processing RemovePlayerAction tab list packet, uuid: {}".format(action.uuid))
                username = mc_uuid_to_username(action.uuid)
                player_uuid = action.uuid
                webhook_payload = {
                    'username': username,
                    'avatar_url':  "https://visage.surgeplay.com/face/160/{}".format(player_uuid),
                    'content': '',
                    'embeds': [{'color': 16711680, 'title': '**Left the game**'}]
                }
                for webhook in WEBHOOKS:
                    post = requests.post(webhook,json=webhook_payload)
                del UUID_CACHE[action.uuid]
                del PLAYER_LIST[action.uuid]
                if config.es_enabled:
                    el.log_connection(uuid=action.uuid, reason=el.ConnectionReason.DISCONNECTED, count=len(PLAYER_LIST))

    def handle_join_game(join_game_packet):
        global PLAYER_LIST
        log.info('Connected.')
        PLAYER_LIST = bidict()

    def handle_chat(chat_packet):
        json_data = json.loads(chat_packet.json_data)
        if "extra" not in json_data:
            return
        chat_string = ""
        for chat_component in json_data["extra"]:
            chat_string += chat_component["text"] 
        
        # Handle chat message
        regexp_match = re.match("<(.*?)> (.*)", chat_string, re.M|re.I)
        if regexp_match:
            username = regexp_match.group(1)
            original_message = regexp_match.group(2)
            player_uuid = mc_username_to_uuid(username)
            if username.lower() == BOT_USERNAME.lower():
                # Don't relay our own messages
                if config.es_enabled:
                    bot_message_match = re.match("<{}> (.*?): (.*)".format(
                        BOT_USERNAME.lower()), chat_string, re.M | re.I)
                    if bot_message_match:
                        el.log_chat_message(
                            uuid=mc_username_to_uuid(bot_message_match.group(1)),
                            display_name=bot_message_match.group(1),
                            message=bot_message_match.group(2),
                            message_unformatted=chat_string)
                        el.log_raw_message(type=ChatType(chat_packet.position).name, message=chat_packet.json_data)
                return
            log.info("Incoming message from minecraft: Username: {} Message: {}".format(username, original_message))
            log.debug("msg: {}".format(repr(original_message)))
            message = escape_markdown(remove_emoji(original_message.strip().replace("@", "@\N{zero width space}")))
            webhook_payload = {
                'username': username,
                'avatar_url':  "https://visage.surgeplay.com/face/160/{}".format(player_uuid),
                'content': '{}'.format(message)
            }
            for webhook in WEBHOOKS:
                post = requests.post(webhook, json=webhook_payload)
            if config.es_enabled:
                el.log_chat_message(
                    uuid=player_uuid, display_name=username, message=original_message, message_unformatted=chat_string)
        if config.es_enabled:
            el.log_raw_message(type=ChatType(chat_packet.position).name, message=chat_packet.json_data)

    def handle_health_update(health_update_packet):
        if health_update_packet.health <= 0:
            log.debug("Respawned the player because it died")
            packet = serverbound.play.ClientStatusPacket()
            packet.action_id = serverbound.play.ClientStatusPacket.RESPAWN
            connection.write_packet(packet)

    register_handlers(connection)

    connection.connect()

    @discord_bot.event
    async def on_ready():
        log.info("Discord bot logged in as {} ({})".format(discord_bot.user.name, discord_bot.user.id))
        global WEBHOOKS
        WEBHOOKS = []
        session = database_session.get_session()
        channels = session.query(DiscordChannel).all()
        session.close()
        for channel in channels:
            channel_id = channel.channel_id
            discord_channel = discord_bot.get_channel(channel_id)
            channel_webhooks = await discord_channel.webhooks()
            found = False
            for webhook in channel_webhooks:
                if webhook.name == "_minecraft":
                    WEBHOOKS.append(webhook.url)
                    found = True
                log.debug("Found webhook {} in channel {}".format(webhook.name, discord_channel.name))
            if not found:
                # Create the hook
                await discord_channel.create_webhook(name="_minecraft")

    @discord_bot.event
    async def on_message(message):
        # We do not want the bot to reply to itself
        if message.author == discord_bot.user:
            return
        this_channel = message.channel.id
        global WEBHOOKS

        # PM Commands
        if message.content.startswith("mc!help"):
            try:
                send_channel = message.channel
                if isinstance(message.channel, discord.abc.GuildChannel):
                    await message.delete()
                    dm_channel = message.author.dm_channel
                    if not dm_channel:
                        await message.author.create_dm()
                    send_channel = message.author.dm_channel
                msg = get_discord_help_string()
                await send_channel.send(msg)
            except discord.errors.Forbidden:
                if isinstance(message.author, discord.abc.User):
                    msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                    error_msg = await message.channel.send(msg)
                    await asyncio.sleep(3)
                    await error_msg.delete()
            finally:
                return

        elif message.content.startswith("mc!register"):
            try:
                # TODO: Catch the Forbidden error in a smart way before running application logic
                send_channel = message.channel
                if isinstance(message.channel, discord.abc.GuildChannel):
                    await message.delete()
                    dm_channel = message.author.dm_channel
                    if not dm_channel:
                        await message.author.create_dm()
                    send_channel = message.author.dm_channel
                session = database_session.get_session()
                discord_account = session.query(DiscordAccount).filter_by(discord_id=message.author.id).first()
                if not discord_account:
                    new_discord_account = DiscordAccount(message.author.id)
                    session.add(new_discord_account)
                    session.commit()
                    discord_account = session.query(DiscordAccount).filter_by(discord_id=message.author.id).first()

                new_token = generate_random_auth_token(16)
                account_link_token = AccountLinkToken(message.author.id, new_token)
                discord_account.link_token = account_link_token
                session.add(account_link_token)
                session.commit()
                msg = "Please connect your minecraft account to `{}.{}:{}` in order to link it to this bridge!"\
                    .format(new_token, config.auth_dns, config.auth_port)
                session.close()
                del session
                await send_channel.send(msg)
            except discord.errors.Forbidden:
                if isinstance(message.author, discord.abc.User):
                    msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                    error_msg = await message.channel.send(msg)
                    await asyncio.sleep(3)
                    await error_msg.delete()
            finally:
                return

        # Global Commands
        elif message.content.startswith("mc!chathere"):
            if isinstance(message.channel, discord.abc.PrivateChannel):
                msg = "Sorry, this command is only available in public channels."
                await message.channel.send(msg)
                return
            if message.author.id not in config.admin_users:
                await message.delete()
                try:
                    dm_channel = message.author.dm_channel
                    if not dm_channel:
                        await message.author.create_dm()
                    dm_channel = message.author.dm_channel
                    msg = "Sorry, you do not have permission to execute that command!"
                    await dm_channel.send(msg)
                except discord.errors.Forbidden:
                    if isinstance(message.author, discord.abc.User):
                        msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                        error_msg = await message.channel.send(msg)
                        await asyncio.sleep(3)
                        await error_msg.delete()
                finally:
                    return
            session = database_session.get_session()
            channels = session.query(DiscordChannel).filter_by(channel_id=this_channel).all()
            if not channels:
                new_channel = DiscordChannel(this_channel)
                session.add(new_channel)
                session.commit()
                session.close()
                del session
                webhook = await message.channel.create_webhook(name="_minecraft")
                WEBHOOKS.append(webhook.url)
                msg = "The bot will now start chatting here! To stop this, run `mc!stopchathere`."
                await message.channel.send(msg)
            else:
                msg = "The bot is already chatting in this channel! To stop this, run `mc!stopchathere`."
                await message.channel.send(msg)
                return

        elif message.content.startswith("mc!stopchathere"):
            if isinstance(message.channel, discord.abc.PrivateChannel):
                msg = "Sorry, this command is only available in public channels."
                await message.channel.send(msg)
                return
            if message.author.id not in config.admin_users:
                await message.delete()
                try:
                    dm_channel = message.author.dm_channel
                    if not dm_channel:
                        await message.author.create_dm()
                    dm_channel = message.author.dm_channel
                    msg = "Sorry, you do not have permission to execute that command!"
                    await dm_channel.send(msg)
                except discord.errors.Forbidden:
                    if isinstance(message.author, discord.abc.User):
                        msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                        error_msg = await message.channel.send(msg)
                        await asyncio.sleep(3)
                        await error_msg.delete()
                finally:
                    return
            session = database_session.get_session()
            deleted = session.query(DiscordChannel).filter_by(channel_id=this_channel).delete()
            session.commit()
            session.close()
            for webhook in message.channel:
                if webhook.name == "_minecraft":
                    del WEBHOOKS[webhook.url]
                    await webhook.delete()
            if deleted < 1:
                msg = "The bot was not chatting here!"
                await message.channel.send(msg)
                return
            else:
                msg = "The bot will no longer here!"
                await message.channel.send(msg)
                return

        elif message.content.startswith("mc!tab"):
            send_channel = message.channel
            try:
                if isinstance(message.channel, discord.abc.GuildChannel):
                    await message.delete()
                    dm_channel = message.author.dm_channel
                    if not dm_channel:
                        await message.author.create_dm()
                    send_channel = message.author.dm_channel
                player_list = ", ".join(list(map(lambda x: x[1], PLAYER_LIST.items())))
                msg = "{}\n" \
                    "Players online: {}\n" \
                    "{}".format(escape_markdown(
                        strip_colour(TAB_HEADER)), escape_markdown(
                        strip_colour(player_list)), escape_markdown(
                        strip_colour(TAB_FOOTER)))
                await send_channel.send(msg)
            except discord.errors.Forbidden:
                if isinstance(message.author, discord.abc.User):
                    msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                    error_msg = await message.channel.send(msg)
                    await asyncio.sleep(3)
                    await error_msg.delete()
            finally:
                return

        elif message.content.startswith("mc!"):
            # Catch-all
            send_channel = message.channel
            try:
                if isinstance(message.channel, discord.abc.GuildChannel):
                    await message.delete()
                    dm_channel = message.author.dm_channel
                    if not dm_channel:
                        await message.author.create_dm()
                    send_channel = message.author.dm_channel
                msg = "Unknown command, type `mc!help` for a list of commands."
                await send_channel.send(msg)
            except discord.errors.Forbidden:
                if isinstance(message.author, discord.abc.User):
                    msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                    error_msg = await message.channel.send(msg)
                    await asyncio.sleep(3)
                    await error_msg.delete()
            finally:
                return
            
        elif not message.author.bot:
            session = database_session.get_session()
            channel_should_chat = session.query(DiscordChannel).filter_by(channel_id=this_channel).first()
            if channel_should_chat:
                await message.delete()
                discord_user = session.query(DiscordAccount).filter_by(discord_id=message.author.id).first()
                if discord_user:
                    if discord_user.minecraft_account:
                        minecraft_uuid = discord_user.minecraft_account.minecraft_uuid
                        session.close()
                        del session
                        minecraft_username = mc_uuid_to_username(minecraft_uuid)

                        # Max chat message length: 256, bot username does not count towards this
                        # Does not count|Counts
                        # <BOT_USERNAME> minecraft_username: message
                        padding = 2 + len(minecraft_username)

                        message_to_send = remove_emoji(
                            message.clean_content.encode('utf-8').decode('ascii', 'replace')).strip()
                        message_to_discord = escape_markdown(message.clean_content)

                        total_len = padding + len(message_to_send)
                        if total_len > 256:
                            message_to_send = message_to_send[:(256 - padding)]
                            message_to_discord = message_to_discord[:(256 - padding)]
                        elif len(message_to_send) <= 0:
                            return

                        session = database_session.get_session()
                        channels = session.query(DiscordChannel).all()
                        session.close()
                        del session
                        global PREVIOUS_MESSAGE, NEXT_MESSAGE_TIME
                        if message_to_send == PREVIOUS_MESSAGE or \
                                datetime.now(timezone.utc) < NEXT_MESSAGE_TIME:
                            send_channel = message.channel
                            try:
                                if isinstance(message.channel, discord.abc.GuildChannel):
                                    dm_channel = message.author.dm_channel
                                    if not dm_channel:
                                        await message.author.create_dm()
                                    send_channel = message.author.dm_channel
                                msg = "Your message \"{}\" has been rate-limited.".format(message.clean_content)
                                await send_channel.send(msg)
                            except discord.errors.Forbidden:
                                if isinstance(message.author, discord.abc.User):
                                    msg = "{}, please allow private messages from this bot.".format(
                                        message.author.mention)
                                    error_msg = await message.channel.send(msg)
                                    await asyncio.sleep(3)
                                    await error_msg.delete()
                            finally:
                                return

                        PREVIOUS_MESSAGE = message_to_send
                        NEXT_MESSAGE_TIME = datetime.now(timezone.utc) + timedelta(seconds=config.message_delay)

                        log.info("Outgoing message from discord: Username: {} Message: {}".format(minecraft_username, message_to_send))

                        for channel in channels:
                            webhooks = await discord_bot.get_channel(channel.channel_id).webhooks()
                            for webhook in webhooks:
                                if webhook.name == "_minecraft":
                                    await webhook.send(
                                        username=minecraft_username,
                                        avatar_url="https://visage.surgeplay.com/face/160/{}".format(minecraft_uuid),
                                        content=message_to_discord)

                        packet = serverbound.play.ChatPacket()
                        packet.message = "{}: {}".format(minecraft_username, message_to_send)
                        connection.write_packet(packet)
                else:
                    send_channel = message.channel
                    try:
                        if isinstance(message.channel, discord.abc.GuildChannel):
                            dm_channel = message.author.dm_channel
                            if not dm_channel:
                                await message.author.create_dm()
                            send_channel = message.author.dm_channel
                        msg = "Unable to send chat message: there is no Minecraft account linked to this discord account," \
                              "please run `mc!register`."
                        await send_channel.send(msg)
                    except discord.errors.Forbidden:
                        if isinstance(message.author, discord.abc.User):
                            msg = "{}, please allow private messages from this bot.".format(message.author.mention)
                            error_msg = await message.channel.send(msg)
                            await asyncio.sleep(3)
                            await error_msg.delete()
                    finally:
                        session.close()
                        del session
                        return
            else:
                session.close()
                del session

    discord_bot.run(config.discord_token)


if __name__ == "__main__":
main()
