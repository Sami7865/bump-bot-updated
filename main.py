import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from pymongo import MongoClient
from datetime import datetime, timedelta
import pytz
from flask import Flask
import threading

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

MONGO_URL = os.getenv("MONGO_URL")
TOKEN = os.getenv("TOKEN")

mongo_client = MongoClient(MONGO_URL)
db = mongo_client["bumpbot"]
config = db["config"]
bumps = db["bumps"]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not check_reminders.is_running():
        check_reminders.start()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if message.embeds:
        for embed in message.embeds:
            if embed.title and "Bump done" in embed.title:
                now = datetime.now(pytz.utc)
                author_id = message.interaction.user.id if message.interaction else message.author.id
                guild_id = str(message.guild.id)
                bumps.insert_one({"user_id": author_id, "timestamp": now, "guild_id": guild_id})

                cfg = config.find_one({"_id": guild_id})
                if cfg and cfg.get("log_channel") and cfg.get("ping_role"):
                    channel = message.guild.get_channel(cfg["log_channel"])
                    role = message.guild.get_role(cfg["ping_role"])

                    next_bump = now + timedelta(hours=2)
                    formatted_time = next_bump.strftime("%Y-%m-%d %H:%M:%S UTC")
                    await channel.send(embed=discord.Embed(
                        description=f"<@{author_id}> bumped the server!\nNext bump available <t:{int(next_bump.timestamp())}:R> (<t:{int(next_bump.timestamp())}:f>)",
                        color=discord.Color.green()
                    ))

                db["last_bump"].update_one(
                    {"_id": guild_id},
                    {"$set": {"timestamp": now, "user_id": author_id}},
                    upsert=True
                )

    await bot.process_commands(message)

@tasks.loop(minutes=1)
async def check_reminders():
    now = datetime.now(pytz.utc)
    for entry in db["last_bump"].find():
        guild = bot.get_guild(int(entry["_id"]))
        if not guild:
            continue

        cfg = config.find_one({"_id": entry["_id"]})
        if not cfg or not cfg.get("log_channel") or not cfg.get("ping_role"):
            continue

        next_bump = entry["timestamp"] + timedelta(hours=2)
        if now >= next_bump:
            channel = guild.get_channel(cfg["log_channel"])
            role = guild.get_role(cfg["ping_role"])
            user = guild.get_member(entry["user_id"])
            if channel and role and user:
                await channel.send(f"{role.mention} <@{user.id}> can bump the server again now!")
                db["last_bump"].delete_one({"_id": entry["_id"]})

# Slash command: /setlogchannel
@tree.command(name="setlogchannel", description="Set the log channel for bump reminders.")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config.update_one({"_id": str(interaction.guild_id)}, {"$set": {"log_channel": channel.id}}, upsert=True)
    await interaction.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)

# Slash command: /setpingrole
@tree.command(name="setpingrole", description="Set the ping role for bump reminders.")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    config.update_one({"_id": str(interaction.guild_id)}, {"$set": {"ping_role": role.id}}, upsert=True)
    await interaction.response.send_message(f"Ping role set to {role.mention}", ephemeral=True)

# Slash command: /bumpstatus
@tree.command(name="bumpstatus", description="Check time remaining for the next bump.")
async def bumpstatus(interaction: discord.Interaction):
    record = db["last_bump"].find_one({"_id": str(interaction.guild_id)})
    if not record:
        await interaction.response.send_message("No bump recorded yet.", ephemeral=True)
        return

    next_bump = record["timestamp"] + timedelta(hours=2)
    now = datetime.now(pytz.utc)
    if now >= next_bump:
        await interaction.response.send_message("You can bump now!", ephemeral=True)
    else:
        seconds = int((next_bump - now).total_seconds())
        await interaction.response.send_message(f"Next bump in <t:{int(next_bump.timestamp())}:R>", ephemeral=True)

# Slash command: /bumphistory
@tree.command(name="bumphistory", description="Show recent bump history for the server.")
async def bumphistory(interaction: discord.Interaction):
    history = list(bumps.find({"guild_id": str(interaction.guild_id)}).sort("timestamp", -1).limit(10))
    if not history:
        await interaction.response.send_message("No bumps recorded yet.", ephemeral=True)
        return

    lines = []
    for bump in history:
        timestamp = bump["timestamp"]
        user_id = bump["user_id"]
        lines.append(f"<@{user_id}> at <t:{int(timestamp.timestamp())}:f>")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# Slash command: /userbumps
@tree.command(name="userbumps", description="Check how many times a user has bumped the server.")
async def userbumps(interaction: discord.Interaction, user: discord.User):
    count = bumps.count_documents({"guild_id": str(interaction.guild_id), "user_id": user.id})
    await interaction.response.send_message(f"{user.mention} has bumped the server {count} times.", ephemeral=True)

# Web server for uptime (UptimeRobot)
app = Flask('')

@app.route('/')
def home():
    return "Bump bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run).start()

bot.run(TOKEN)
