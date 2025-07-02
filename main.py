import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
import asyncio
import os
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

mongo_client = MongoClient(os.environ['MONGO_URI'])
db = mongo_client['bumpbot']
collection = db['bumps']
settings = db['settings']

SCAN_KEYWORD = "bump done"
SCAN_INTERVAL = 120  # default to 2 minutes
scanner_tasks = {}

# Flask server for UptimeRobot
app = Flask(__name__)
@app.route('/')
def home():
    return "Bump bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()

def get_time_left(last_bump):
    time_passed = datetime.utcnow() - last_bump
    time_left = timedelta(hours=2) - time_passed
    return max(time_left, timedelta())

async def log_message(guild_id, content):
    config = settings.find_one({"_id": guild_id})
    if config and "log_channel" in config:
        channel = bot.get_channel(config["log_channel"])
        if channel:
            await channel.send(content)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Error syncing commands: {e}")

    # Restart all scanners
    for config in settings.find():
        if config.get("scanner_on", False):
            guild = bot.get_guild(config["_id"])
            if guild:
                channel_id = config.get("scanner_channel")
                interval = config.get("scanner_interval", SCAN_INTERVAL)
                if channel_id:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        start_scanner(guild.id, channel, interval)

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    if message.author.bot or not message.guild:
        return
    config = settings.find_one({"_id": message.guild.id})
    if config and message.channel.id == config.get("scanner_channel"):
        if SCAN_KEYWORD in message.content.lower():
            await handle_bump(message.author, message.guild)

async def handle_bump(user, guild):
    now = datetime.utcnow()
    previous = collection.find_one({"_id": guild.id})
    if previous:
        collection.update_one({"_id": guild.id}, {"$set": {"user": user.id, "time": now}})
    else:
        collection.insert_one({"_id": guild.id, "user": user.id, "time": now})
    db['history'].insert_one({
        "guild": guild.id,
        "user": user.id,
        "time": now
    })
    await log_message(guild.id, f"🟢 {user.mention} just bumped the server!\nNext bump available <t:{int((now + timedelta(hours=2)).timestamp())}:R>")
    asyncio.create_task(schedule_reminder(guild.id, user.id, now))

async def schedule_reminder(guild_id, user_id, bump_time):
    await asyncio.sleep(7200)  # 2 hours
    current = collection.find_one({"_id": guild_id})
    if current and current["user"] == user_id and current["time"] == bump_time:
        config = settings.find_one({"_id": guild_id})
        if config and "ping_role" in config:
            role_id = config["ping_role"]
            role = bot.get_guild(guild_id).get_role(role_id)
            await log_message(guild_id, f"🔔 {role.mention} Time to bump again!")
        else:
            await log_message(guild_id, "🔔 Time to bump again!")

@tree.command(name="bumpstatus", description="Check time left for next bump.")
async def bump_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    doc = collection.find_one({"_id": interaction.guild.id})
    if doc:
        time_left = get_time_left(doc["time"])
        if time_left.total_seconds() <= 0:
            await interaction.followup.send("✅ You can bump now!", ephemeral=True)
        else:
            mins = int(time_left.total_seconds() // 60)
            await interaction.followup.send(f"⏳ Time left to bump again: {mins} minutes", ephemeral=True)
    else:
        await interaction.followup.send("✅ No active bump timer. You can bump now!", ephemeral=True)

@tree.command(name="resetbump", description="Reset the bump timer.")
@app_commands.checks.has_permissions(administrator=True)
async def reset_bump(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    collection.delete_one({"_id": interaction.guild.id})
    await log_message(interaction.guild.id, f"🔄 Bump timer was reset by {interaction.user.mention}")
    await interaction.followup.send("✅ Bump timer reset!", ephemeral=True)

@tree.command(name="bumphistory", description="View recent bumpers.")
async def bumphistory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    history = db['history'].find({"guild": interaction.guild.id}).sort("time", -1).limit(5)
    msg = ""
    for record in history:
        user = interaction.guild.get_member(record["user"])
        time_str = f"<t:{int(record['time'].timestamp())}:R>"
        msg += f"- {user.mention if user else 'Unknown'} • {time_str}\n"
    await interaction.followup.send(f"📜 Last bumps:\n{msg}", ephemeral=True)

@tree.command(name="userbumps", description="Check how many times a user has bumped.")
@app_commands.describe(user="Select a user")
async def userbumps(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    count = db['history'].count_documents({"guild": interaction.guild.id, "user": user.id})
    await interaction.followup.send(f"🔢 {user.mention} has bumped **{count}** times.", ephemeral=True)

@tree.command(name="setlogchannel", description="Set the log channel.")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"log_channel": interaction.channel.id}}, upsert=True)
    await interaction.followup.send(f"📘 This channel has been set as the log channel.", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role to ping for bump reminders.")
@app_commands.describe(role="Role to ping")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"ping_role": role.id}}, upsert=True)
    await interaction.followup.send(f"🔔 Bump ping role set to {role.mention}", ephemeral=True)

@tree.command(name="setscannerchannel", description="Set the bump scanner channel.")
@app_commands.describe(channel="Channel to scan for 'bump done'")
@app_commands.checks.has_permissions(administrator=True)
async def setscannerchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"scanner_channel": channel.id}}, upsert=True)
    await interaction.followup.send(f"🔍 Scanner channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setscannerinterval", description="Set scan interval in seconds.")
@app_commands.describe(seconds="Interval in seconds")
@app_commands.checks.has_permissions(administrator=True)
async def setscannerinterval(interaction: discord.Interaction, seconds: int):
    await interaction.response.defer(ephemeral=True)
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"scanner_interval": seconds}}, upsert=True)
    await interaction.followup.send(f"⏱️ Scanner interval set to {seconds} seconds.", ephemeral=True)
    config = settings.find_one({"_id": interaction.guild.id})
    if config.get("scanner_on", False):
        channel_id = config.get("scanner_channel")
        if channel_id:
            start_scanner(interaction.guild.id, bot.get_channel(channel_id), seconds)

@tree.command(name="togglescanner", description="Turn the bump scanner on or off.")
@app_commands.checks.has_permissions(administrator=True)
async def togglescanner(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    config = settings.find_one({"_id": interaction.guild.id}) or {}
    on = not config.get("scanner_on", False)
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"scanner_on": on}}, upsert=True)
    if on:
        channel_id = config.get("scanner_channel")
        interval = config.get("scanner_interval", SCAN_INTERVAL)
        if channel_id:
            start_scanner(interaction.guild.id, bot.get_channel(channel_id), interval)
        await interaction.followup.send("✅ Scanner turned **ON**.", ephemeral=True)
    else:
        stop_scanner(interaction.guild.id)
        await interaction.followup.send("🛑 Scanner turned **OFF**.", ephemeral=True)

def start_scanner(guild_id, channel, interval):
    async def scan_loop():
        await bot.wait_until_ready()
        while True:
            if not bot.is_closed():
                async for message in channel.history(limit=10):
                    if SCAN_KEYWORD in message.content.lower() and not message.author.bot:
                        await handle_bump(message.author, message.guild)
                        break
                await asyncio.sleep(interval)
            else:
                break
    stop_scanner(guild_id)
    task = asyncio.create_task(scan_loop())
    scanner_tasks[guild_id] = task

def stop_scanner(guild_id):
    task = scanner_tasks.get(guild_id)
    if task:
        task.cancel()
        del scanner_tasks[guild_id]

bot.run(os.environ['DISCORD_TOKEN'])
