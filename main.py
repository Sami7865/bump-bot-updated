import discord
from discord.ext import tasks
from discord import app_commands
from discord.ext.commands import Bot
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
import asyncio
import os
from flask import Flask

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = Bot(command_prefix="!", intents=intents)
tree = bot.tree

TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URL = os.environ.get("MONGO_URI")

client = MongoClient(MONGO_URL)
db = client["bumpbot"]
settings = db["settings"]
bumps = db["bumps"]

scanner_intervals = {}  # {guild_id: seconds}
bump_timers = {}        # {guild_id: {"user_id": ..., "time": datetime}}
scanner_tasks = {}      # {guild_id: task}

@bot.event
async def on_ready():
    print(f"Bot is ready. Logged in as {bot.user}.")
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    for guild in bot.guilds:
        config = settings.find_one({"guild_id": guild.id}) or {}
        scanner_channel_id = config.get("scanner_channel")
        interval = config.get("scanner_interval", 120)
        if scanner_channel_id:
            scanner_intervals[guild.id] = interval
            task = create_scanner_task(guild.id, scanner_channel_id, interval)
            scanner_tasks[guild.id] = task
            task.start()

def create_scanner_task(guild_id, channel_id, interval):
    @tasks.loop(seconds=interval)
    async def scanner():
        try:
            guild = bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id)
            if not channel:
                return
            async for message in channel.history(limit=10):
                if (
                    message.embeds
                    and "Bump done!" in message.embeds[0].description
                    and message.author.id == 302050872383242240
                ):
                    user = message.interaction.user if message.interaction else message.author
                    await handle_bump(guild, user)
                    break
        except Exception as e:
            print(f"[Scanner Error] Guild {guild_id}: {e}")
    return scanner

async def handle_bump(guild, user):
    now = datetime.now(timezone.utc)
    bump_timers[guild.id] = {"user_id": user.id, "time": now}
    config = settings.find_one({"guild_id": guild.id}) or {}
    log_channel_id = config.get("log_channel")
    ping_role_id = config.get("ping_role")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            next_time = now + timedelta(hours=2)
            await log_channel.send(
                f"📌 {user.mention} just bumped the server!\nNext bump: <t:{int(next_time.timestamp())}:R>"
            )
    bumps.insert_one({
        "guild_id": guild.id,
        "user_id": user.id,
        "timestamp": now
    })
    asyncio.create_task(schedule_reminder(guild, user, now))

async def schedule_reminder(guild, user, start_time):
    await asyncio.sleep(7200)
    latest = bump_timers.get(guild.id)
    if not latest or latest["user_id"] != user.id or latest["time"] != start_time:
        return
    config = settings.find_one({"guild_id": guild.id}) or {}
    log_channel_id = config.get("log_channel")
    ping_role_id = config.get("ping_role")
    if log_channel_id and ping_role_id:
        log_channel = guild.get_channel(log_channel_id)
        ping_role = guild.get_role(ping_role_id)
        if log_channel and ping_role:
            await log_channel.send(f"🔔 {ping_role.mention}, <@{user.id}> it's time to bump the server again!")

@tree.command(description="Check time left for next bump")
async def bumpstatus(interaction: discord.Interaction):
    data = bump_timers.get(interaction.guild.id)
    if data:
        next_time = data["time"] + timedelta(hours=2)
        now = datetime.now(timezone.utc)
        if next_time > now:
            remaining = int((next_time - now).total_seconds())
            await interaction.response.send_message(f"⏳ Next bump in <t:{int(next_time.timestamp())}:R>", ephemeral=True)
            return
    await interaction.response.send_message("✅ No active bump timer. You can bump now!", ephemeral=True)

@tree.command(description="Reset the current bump timer")
@app_commands.checks.has_permissions(administrator=True)
async def resetbump(interaction: discord.Interaction):
    bump_timers.pop(interaction.guild.id, None)
    await interaction.response.send_message("🧹 Bump timer reset!", ephemeral=True)
    config = settings.find_one({"guild_id": interaction.guild.id}) or {}
    log_channel_id = config.get("log_channel")
    if log_channel_id:
        log_channel = interaction.guild.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(f"🧹 Bump timer was reset by {interaction.user.mention}.")

@tree.command(description="Show bump history in this server")
async def bumphistory(interaction: discord.Interaction):
    records = list(bumps.find({"guild_id": interaction.guild.id}).sort("timestamp", -1).limit(10))
    if not records:
        await interaction.response.send_message("No bumps recorded yet.")
        return
    lines = []
    for r in records:
        user = interaction.guild.get_member(r["user_id"])
        time_str = f"<t:{int(r['timestamp'].timestamp())}:R>"
        lines.append(f"{user.mention if user else 'Unknown'} — {time_str}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@tree.command(description="Set log channel for bump reminders and logs")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings.update_one({"guild_id": interaction.guild.id}, {"$set": {"log_channel": channel.id}}, upsert=True)
    await interaction.response.send_message(f"📌 Log channel set to {channel.mention}", ephemeral=True)

@tree.command(description="Set role to ping for bump reminders")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    settings.update_one({"guild_id": interaction.guild.id}, {"$set": {"ping_role": role.id}}, upsert=True)
    await interaction.response.send_message(f"🔔 Ping role set to {role.mention}", ephemeral=True)

@tree.command(description="Set the scanner channel for bump detection")
@app_commands.checks.has_permissions(administrator=True)
async def setscannerchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = interaction.guild.id
    settings.update_one({"guild_id": guild_id}, {"$set": {"scanner_channel": channel.id}}, upsert=True)
    if guild_id in scanner_tasks:
        scanner_tasks[guild_id].cancel()
    interval = scanner_intervals.get(guild_id, 120)
    task = create_scanner_task(guild_id, channel.id, interval)
    scanner_tasks[guild_id] = task
    task.start()
    await interaction.response.send_message(f"📡 Scanner channel set to {channel.mention}", ephemeral=True)

@tree.command(description="Set scanner interval in seconds")
@app_commands.checks.has_permissions(administrator=True)
async def setscannerinterval(interaction: discord.Interaction, seconds: int):
    if seconds < 10 or seconds > 600:
        await interaction.response.send_message("❌ Interval must be between 10 and 600 seconds.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    scanner_intervals[guild_id] = seconds
    settings.update_one({"guild_id": guild_id}, {"$set": {"scanner_interval": seconds}}, upsert=True)
    if guild_id in scanner_tasks:
        scanner_tasks[guild_id].change_interval(seconds=seconds)
    await interaction.response.send_message(f"⏱️ Scanner interval set to {seconds} seconds", ephemeral=True)

# Optional: Flask for uptime
app = Flask("")

@app.route("/")
def home():
    return "OK"

def run_web():
    app.run(host="0.0.0.0", port=8080)

import threading
threading.Thread(target=run_web).start()

bot.run(TOKEN)
