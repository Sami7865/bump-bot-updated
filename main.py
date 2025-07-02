import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
from datetime import datetime, timedelta
import os
from flask import Flask
import threading

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# MongoDB
mongo = MongoClient(os.environ["MONGO_URI"])
db = mongo["bumpbot"]
settings = db["settings"]
bumps = db["bumps"]

SCAN_KEYWORD = "bump done"
scanner_tasks = {}

# Flask for UptimeRobot
app = Flask("")

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()

# Helper
def get_next_bump_time(last_bump):
    return (last_bump + timedelta(hours=2)).strftime('%H:%M UTC')

async def handle_bump(user, guild):
    now = datetime.utcnow()
    config = settings.find_one({"_id": guild.id}) or {}

    bumps.update_one(
        {"_id": guild.id},
        {"$set": {"last_bump": now, "bumper_id": user.id}, "$push": {"history": {"user": user.id, "time": now}}},
        upsert=True
    )

    log_channel_id = config.get("log_channel")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            next_bump = get_next_bump_time(now)
            await log_channel.send(f"📌 {user.mention} bumped the server!\nNext bump at **{next_bump}**")

    async def reminder():
        await discord.utils.sleep_until(now + timedelta(hours=2))
        config = settings.find_one({"_id": guild.id})
        last = bumps.find_one({"_id": guild.id})
        if not last or last.get("last_bump") != now:
            return  # Someone else bumped later

        role_id = config.get("ping_role")
        log_channel = guild.get_channel(config.get("log_channel"))
        if role_id and log_channel:
            role = guild.get_role(role_id)
            if role:
                await log_channel.send(f"🔔 {user.mention} it's time to bump again! {role.mention}")

    bot.loop.create_task(reminder())

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()
    for guild in bot.guilds:
        config = settings.find_one({"_id": guild.id})
        if config and config.get("scanner_on", True):
            start_scanner(guild)

@bot.event
async def on_guild_join(guild):
    settings.update_one({"_id": guild.id}, {"$setOnInsert": {"scanner_interval": 30, "scanner_on": True}}, upsert=True)

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    if message.author.bot or not message.guild:
        return

    config = settings.find_one({"_id": message.guild.id}) or {}

    # ✅ Detect Disboard embed bumps
    if message.embeds:
        embed = message.embeds[0]
        if (
            message.author.id == 302050872383242240 and  # DISBOARD Bot ID
            embed.title and "DISBOARD" in embed.title.upper() and
            "bump done" in embed.description.lower()
        ):
            ref = await message.channel.fetch_message(message.reference.message_id) if message.reference else None
            bumper = ref.author if ref else None
            if bumper:
                await handle_bump(bumper, message.guild)

# Scanner
def start_scanner(guild):
    async def scan():
        await bot.wait_until_ready()
        config = settings.find_one({"_id": guild.id})
        interval = config.get("scanner_interval", 30)
        channel = guild.get_channel(config.get("scanner_channel"))
        if not channel:
            return

        last_message = None
        while settings.find_one({"_id": guild.id}).get("scanner_on", True):
            try:
                messages = [msg async for msg in channel.history(limit=5)]
                for msg in messages:
                    if (
                        msg.author.id == 302050872383242240 and
                        msg.embeds and
                        "bump done" in msg.embeds[0].description.lower()
                    ):
                        ref = await msg.channel.fetch_message(msg.reference.message_id) if msg.reference else None
                        bumper = ref.author if ref else None
                        if bumper:
                            await handle_bump(bumper, guild)
                            break
                await discord.utils.sleep_until(datetime.utcnow() + timedelta(seconds=interval))
            except Exception as e:
                print(f"Scanner error: {e}")
                break

    task = bot.loop.create_task(scan())
    scanner_tasks[guild.id] = task

def stop_scanner(guild):
    task = scanner_tasks.pop(guild.id, None)
    if task:
        task.cancel()

# Slash Commands
@tree.command(description="Set the log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"log_channel": channel.id}}, upsert=True)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(description="Set the ping role")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"ping_role": role.id}}, upsert=True)
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(description="Set the scanner channel")
@app_commands.checks.has_permissions(administrator=True)
async def setscannerchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"scanner_channel": channel.id}}, upsert=True)
    await interaction.response.send_message(f"✅ Scanner channel set to {channel.mention}", ephemeral=True)

@tree.command(description="Set the scanner interval (in seconds)")
@app_commands.checks.has_permissions(administrator=True)
async def setscannerinterval(interaction: discord.Interaction, seconds: int):
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"scanner_interval": seconds}}, upsert=True)
    await interaction.response.send_message(f"✅ Scanner interval set to {seconds} seconds", ephemeral=True)

@tree.command(description="Toggle scanner on/off")
@app_commands.checks.has_permissions(administrator=True)
async def togglescanner(interaction: discord.Interaction):
    config = settings.find_one({"_id": interaction.guild.id}) or {}
    enabled = not config.get("scanner_on", True)
    settings.update_one({"_id": interaction.guild.id}, {"$set": {"scanner_on": enabled}}, upsert=True)
    if enabled:
        start_scanner(interaction.guild)
        await interaction.response.send_message("🟢 Scanner turned **ON**", ephemeral=True)
    else:
        stop_scanner(interaction.guild)
        await interaction.response.send_message("🔴 Scanner turned **OFF**", ephemeral=True)

@tree.command(description="Check time left for next bump")
async def bumpstatus(interaction: discord.Interaction):
    record = bumps.find_one({"_id": interaction.guild.id})
    if not record or "last_bump" not in record:
        await interaction.response.send_message("No bumps recorded yet.", ephemeral=True)
        return
    next_bump = record["last_bump"] + timedelta(hours=2)
    remaining = next_bump - datetime.utcnow()
    if remaining.total_seconds() > 0:
        minutes = int(remaining.total_seconds() // 60)
        await interaction.response.send_message(f"⏳ Next bump in {minutes} minutes", ephemeral=True)
    else:
        await interaction.response.send_message("✅ You can bump now!", ephemeral=True)

@tree.command(description="Reset current bump tracking")
@app_commands.checks.has_permissions(administrator=True)
async def resetbump(interaction: discord.Interaction):
    bumps.update_one({"_id": interaction.guild.id}, {"$unset": {"last_bump": "", "bumper_id": ""}})
    config = settings.find_one({"_id": interaction.guild.id})
    log_channel = interaction.guild.get_channel(config.get("log_channel")) if config else None
    if log_channel:
        await log_channel.send(f"🔄 Bump timer has been reset by {interaction.user.mention}")
    await interaction.response.send_message("🔁 Bump tracking has been reset.", ephemeral=True)

@tree.command(description="Show recent bump history")
async def bumphistory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    record = bumps.find_one({"_id": interaction.guild.id})
    if not record or "history" not in record:
        await interaction.followup.send("No bump history found.")
        return
    history = record["history"][-5:]
    lines = []
    for entry in reversed(history):
        user = interaction.guild.get_member(entry["user"])
        time = entry["time"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"• {user.mention if user else 'Unknown'} at {time}")
    await interaction.followup.send("📜 **Last bumps:**\n" + "\n".join(lines))

@tree.command(description="Show bump count for a user")
async def userbumps(interaction: discord.Interaction, user: discord.Member):
    record = bumps.find_one({"_id": interaction.guild.id})
    count = sum(1 for entry in record.get("history", []) if entry["user"] == user.id)
    await interaction.response.send_message(f"📈 {user.mention} has bumped {count} time(s)", ephemeral=True)

bot.run(os.environ["TOKEN"])
