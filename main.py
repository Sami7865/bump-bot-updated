import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
import datetime
import asyncio
import os
from flask import Flask

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

TOKEN = os.environ['DISCORD_BOT_TOKEN']
MONGO_URI = os.environ['MONGO_URI']

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["bumpbot"]
config_collection = db["config"]
bump_collection = db["bumps"]

scan_channels = {}
scan_intervals = {}
scanner_status = {}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print("Failed to sync commands:", e)
    bump_scanner.start()

def get_config(guild_id):
    return config_collection.find_one({"_id": str(guild_id)})

def update_config(guild_id, data):
    config_collection.update_one({"_id": str(guild_id)}, {"$set": data}, upsert=True)

@tree.command(description="Show time left until next bump")
async def bumpstatus(interaction: discord.Interaction):
    config = get_config(interaction.guild.id)
    if config and "last_bump" in config:
        delta = datetime.datetime.utcnow() - config["last_bump"]
        if delta.total_seconds() < 7200:
            remaining = 7200 - delta.total_seconds()
            m, s = divmod(int(remaining), 60)
            h, m = divmod(m, 60)
            await interaction.response.send_message(
                f"⏳ Next bump available in {h}h {m}m {s}s", ephemeral=True
            )
            return
    await interaction.response.send_message("✅ No active bump timer. You can bump now!", ephemeral=True)

@tree.command(description="Set the log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    update_config(interaction.guild.id, {"log_channel": channel.id})
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(description="Set the role to ping on reminder")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    update_config(interaction.guild.id, {"ping_role": role.id})
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(description="Reset bump timer manually")
@app_commands.checks.has_permissions(administrator=True)
async def resetbump(interaction: discord.Interaction):
    update_config(interaction.guild.id, {"last_bump": None, "bumper_id": None})
    await interaction.response.send_message("🔄 Bump timer has been reset.")
    config = get_config(interaction.guild.id)
    if config and "log_channel" in config:
        channel = bot.get_channel(config["log_channel"])
        if channel:
            await channel.send("🔄 **Bump timer has been manually reset.**")

@tree.command(description="Show bump history for this server")
async def bumphistory(interaction: discord.Interaction):
    records = list(bump_collection.find({"guild_id": interaction.guild.id}).sort("timestamp", -1).limit(10))
    if not records:
        await interaction.response.send_message("📭 No bump history found.")
        return
    msg = "\n".join(
        f"<t:{int(record['timestamp'].timestamp())}:R> - <@{record['user_id']}>"
        for record in records
    )
    await interaction.response.send_message(f"📜 Last bumps:\n{msg}")

@tree.command(description="Show bump history of a specific user")
@app_commands.describe(user="The user to show bump history for")
async def userbumps(interaction: discord.Interaction, user: discord.User):
    records = list(bump_collection.find({
        "guild_id": interaction.guild.id,
        "user_id": user.id
    }).sort("timestamp", -1).limit(10))
    if not records:
        await interaction.response.send_message(f"📭 No bump history for {user.mention}")
        return
    msg = "\n".join(f"<t:{int(record['timestamp'].timestamp())}:R>" for record in records)
    await interaction.response.send_message(f"📜 Last bumps by {user.mention}:\n{msg}")

@tree.command(description="Set the channel to scan for bump messages")
@app_commands.checks.has_permissions(administrator=True)
async def setscanchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    update_config(interaction.guild.id, {"scan_channel": channel.id})
    await interaction.response.send_message(f"🔎 Scanner will watch {channel.mention}")

@tree.command(description="Set how often the scanner checks for bumps (in seconds)")
@app_commands.checks.has_permissions(administrator=True)
async def setscaninterval(interaction: discord.Interaction, seconds: int):
    update_config(interaction.guild.id, {"scan_interval": seconds})
    await interaction.response.send_message(f"⏱️ Scanner interval set to {seconds} seconds")

@tree.command(description="Turn bump scanner on or off")
@app_commands.checks.has_permissions(administrator=True)
async def togglescanner(interaction: discord.Interaction, status: str):
    status = status.lower()
    if status not in ["on", "off"]:
        await interaction.response.send_message("❌ Use 'on' or 'off'")
        return
    update_config(interaction.guild.id, {"scanner_enabled": (status == "on")})
    await interaction.response.send_message(f"🧭 Scanner turned {'on' if status == 'on' else 'off'}")

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    config = get_config(message.guild.id)
    if not config or "scan_channel" not in config:
        return
    if message.channel.id != config["scan_channel"]:
        return
    if "disboard.org" in message.content or any(embed.title == "Bump done!" for embed in message.embeds if embed.title):
        await handle_bump(message.guild, message.author)

async def handle_bump(guild, user):
    now = datetime.datetime.utcnow()
    update_config(guild.id, {
        "last_bump": now,
        "bumper_id": user.id
    })
    bump_collection.insert_one({
        "guild_id": guild.id,
        "user_id": user.id,
        "timestamp": now
    })

    config = get_config(guild.id)
    if config and "log_channel" in config:
        log_channel = bot.get_channel(config["log_channel"])
        if log_channel:
            role_mention = f"<@&{config['ping_role']}>" if "ping_role" in config else ""
            await log_channel.send(f"✅ {user.mention} just bumped the server! {role_mention}\nNext bump <t:{int((now + datetime.timedelta(hours=2)).timestamp())}:R>")

    # Reminder task
    await asyncio.sleep(7200)
    # If still the same bumper
    updated = get_config(guild.id)
    if updated and updated.get("bumper_id") == user.id:
        if "log_channel" in updated:
            log_channel = bot.get_channel(updated["log_channel"])
            if log_channel:
                role_mention = f"<@&{updated['ping_role']}>" if "ping_role" in updated else ""
                await log_channel.send(f"🔔 {role_mention} Time to bump again!")

@tasks.loop(seconds=60)
async def bump_scanner():
    for guild in bot.guilds:
        config = get_config(guild.id)
        if not config or not config.get("scanner_enabled"):
            continue
        channel_id = config.get("scan_channel")
        interval = config.get("scan_interval", 120)
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        try:
            async for message in channel.history(limit=10):
                if message.author.bot and (
                    "disboard.org" in message.content or
                    any(embed.title == "Bump done!" for embed in message.embeds if embed.title)
                ):
                    await handle_bump(guild, message.author)
                    break
        except Exception as e:
            print(f"[Scanner Error] {guild.name}: {e}")
        await asyncio.sleep(interval)

# Uptime web server (optional)
app = Flask("")

@app.route("/")
def home():
    return "Online!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

import threading
threading.Thread(target=run_flask).start()

bot.run(TOKEN)
