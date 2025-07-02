import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
from pymongo import MongoClient
import os
from flask import Flask
import threading

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# MongoDB setup
mongo_url = os.environ["MONGO_URL"]
mongo_client = MongoClient(mongo_url)
db = mongo_client["bump_bot"]
settings_collection = db["settings"]
bumps_collection = db["bumps"]

reminders = {}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    scan_bump_channel.start()
    await tree.sync()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/bump"))

def get_guild_settings(guild_id):
    settings = settings_collection.find_one({"_id": guild_id})
    if not settings:
        settings = {
            "_id": guild_id,
            "log_channel": None,
            "ping_role": None,
            "scanner_channel": None,
            "scanner_interval": 120
        }
        settings_collection.insert_one(settings)
    return settings

def update_setting(guild_id, key, value):
    settings_collection.update_one({"_id": guild_id}, {"$set": {key: value}}, upsert=True)

def log_to_channel(guild, message):
    settings = get_guild_settings(guild.id)
    if settings.get("log_channel"):
        log_channel = guild.get_channel(settings["log_channel"])
        if log_channel:
            return log_channel.send(message)

async def start_reminder(message, user):
    guild_id = message.guild.id
    now = datetime.datetime.utcnow()
    reminders[guild_id] = {"time": now, "user": user.id}
    bumps_collection.insert_one({
        "guild_id": guild_id,
        "user_id": user.id,
        "timestamp": now
    })
    log_to_channel(message.guild, f"✅ Bump by {user.mention} at <t:{int(now.timestamp())}:f>.")

    await discord.utils.sleep_until(now + datetime.timedelta(hours=2))
    updated = reminders.get(guild_id)
    if updated and updated["user"] == user.id:
        settings = get_guild_settings(guild_id)
        role = message.guild.get_role(settings.get("ping_role"))
        log_to_channel(message.guild, f"⏰ Bump reminder for {user.mention}!")
        if role:
            await message.channel.send(f"{role.mention} ⏰ It's time to bump again!")

@bot.event
async def on_message(message):
    if message.author.bot and message.embeds:
        embed = message.embeds[0]
        if embed.title == "DISBOARD: The Public Server List" and "Bump done!" in embed.description:
            if message.guild:
                await start_reminder(message, message.interaction.user if message.interaction else message.mentions[0] if message.mentions else message.author)

@tree.command(name="bumpstatus")
async def bump_status(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    data = reminders.get(guild_id)
    if not data:
        await interaction.response.send_message("✅ No active bump timer. You can bump now!", ephemeral=True)
        return
    remaining = data["time"] + datetime.timedelta(hours=2) - datetime.datetime.utcnow()
    minutes = int(remaining.total_seconds() / 60)
    await interaction.response.send_message(f"⏳ Next bump in {minutes} minutes by <@{data['user']}>", ephemeral=True)

@tree.command(name="resetbump")
@app_commands.checks.has_permissions(administrator=True)
async def reset_bump(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    reminders.pop(guild_id, None)
    log_to_channel(interaction.guild, f"🔄 Bump timer reset by {interaction.user.mention}.")
    await interaction.response.send_message("✅ Bump timer has been reset.", ephemeral=True)

@tree.command(name="bumphistory")
async def bump_history(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    records = bumps_collection.find({"guild_id": guild_id}).sort("timestamp", -1).limit(10)
    record_list = list(records)
    if not record_list:
        await interaction.response.send_message("❌ No bump history found.", ephemeral=True)
        return
    lines = []
    for r in record_list:
        timestamp = int(r["timestamp"].timestamp())
        lines.append(f"<@{r['user_id']}> - <t:{timestamp}:R>")
    await interaction.response.send_message("📜 Recent bumps:\n" + "\n".join(lines), ephemeral=True)

@tree.command(name="userbumps")
async def user_bumps(interaction: discord.Interaction, member: discord.Member):
    guild_id = interaction.guild.id
    count = bumps_collection.count_documents({"guild_id": guild_id, "user_id": member.id})
    await interaction.response.send_message(f"📊 {member.mention} has bumped {count} times.", ephemeral=True)

@tree.command(name="setlogchannel")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    update_setting(interaction.guild.id, "log_channel", channel.id)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole")
@app_commands.checks.has_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    update_setting(interaction.guild.id, "ping_role", role.id)
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(name="setscannerchannel")
@app_commands.checks.has_permissions(administrator=True)
async def set_scanner_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    update_setting(interaction.guild.id, "scanner_channel", channel.id)
    await interaction.response.send_message(f"✅ Scanner channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setscannerinterval")
@app_commands.checks.has_permissions(administrator=True)
async def set_scanner_interval(interaction: discord.Interaction, seconds: int):
    update_setting(interaction.guild.id, "scanner_interval", seconds)
    scan_bump_channel.restart()
    await interaction.response.send_message(f"✅ Scanner interval set to {seconds} seconds.", ephemeral=True)

@tasks.loop(seconds=60)
async def scan_bump_channel():
    for guild in bot.guilds:
        settings = get_guild_settings(guild.id)
        channel_id = settings.get("scanner_channel")
        interval = settings.get("scanner_interval", 120)
        scan_bump_channel.change_interval(seconds=interval)
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not channel:
            continue
        try:
            messages = [msg async for msg in channel.history(limit=10)]
            for msg in messages:
                if msg.author.bot and msg.embeds:
                    embed = msg.embeds[0]
                    if embed.title == "DISBOARD: The Public Server List" and "Bump done!" in embed.description:
                        await start_reminder(msg, msg.interaction.user if msg.interaction else msg.mentions[0] if msg.mentions else msg.author)
                        break
        except Exception as e:
            print(f"Error scanning {guild.name}: {e}")

# Web server to keep Render alive
app = Flask("")

@app.route("/")
def home():
    return "Bot is running!"

def run():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run).start()

bot.run(os.environ["DISCORD_TOKEN"])
