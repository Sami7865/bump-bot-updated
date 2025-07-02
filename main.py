import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
import datetime
import asyncio
import os

TOKEN = os.environ['TOKEN']
MONGO_URI = os.environ['MONGO_URI']

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

mongo = MongoClient(MONGO_URI)
db = mongo['bumpbot']
config_col = db['config']
bump_col = db['bumps']

reminders = {}

@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Failed to sync: {e}")
    scanner_loop.start()

@tree.command(name="setlogchannel", description="Set the channel where bump logs are sent.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_col.update_one({"_id": interaction.guild_id}, {"$set": {"log_channel": channel.id}}, upsert=True)
    await interaction.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role to ping for bump reminders.")
@app_commands.checks.has_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    config_col.update_one({"_id": interaction.guild_id}, {"$set": {"ping_role": role.id}}, upsert=True)
    await interaction.response.send_message(f"Ping role set to {role.mention}", ephemeral=True)

@tree.command(name="setscanchannel", description="Set the channel to scan for Disboard bump messages.")
@app_commands.checks.has_permissions(administrator=True)
async def set_scan_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_col.update_one({"_id": interaction.guild_id}, {"$set": {"scan_channel": channel.id}}, upsert=True)
    await interaction.response.send_message(f"Scan channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setscannerinterval", description="Set how often the scanner checks for bumps (in seconds).")
@app_commands.checks.has_permissions(administrator=True)
async def set_scanner_interval(interaction: discord.Interaction, seconds: int):
    if seconds < 15:
        return await interaction.response.send_message("Minimum scanner interval is 15 seconds.", ephemeral=True)
    config_col.update_one({"_id": interaction.guild_id}, {"$set": {"scanner_interval": seconds}}, upsert=True)
    await interaction.response.send_message(f"Scanner interval set to {seconds} seconds.", ephemeral=True)

@tree.command(name="bumpstatus", description="Check time left for next bump.")
async def bump_status(interaction: discord.Interaction):
    data = reminders.get(interaction.guild_id)
    if data:
        remaining = int((data["next_bump"] - datetime.datetime.utcnow()).total_seconds())
        minutes = remaining // 60
        seconds = remaining % 60
        await interaction.response.send_message(f"⏳ Next bump available in {minutes}m {seconds}s", ephemeral=True)
    else:
        await interaction.response.send_message("✅ No active bump timer. You can bump now!", ephemeral=True)

@tree.command(name="resetbump", description="Manually reset bump timer.")
@app_commands.checks.has_permissions(administrator=True)
async def reset_bump(interaction: discord.Interaction):
    reminders.pop(interaction.guild_id, None)
    log_channel_id = config_col.find_one({"_id": interaction.guild_id}, {"log_channel": 1}) or {}
    log_channel = bot.get_channel(log_channel_id.get("log_channel", 0))
    if log_channel:
        await log_channel.send(f"🔁 {interaction.user.mention} manually reset the bump timer.")
    await interaction.response.send_message("Bump timer reset manually.", ephemeral=True)

@tree.command(name="bumphistory", description="Show recent bumps.")
async def bump_history(interaction: discord.Interaction):
    records = bump_col.find({"guild_id": interaction.guild_id}).sort("timestamp", -1).limit(10)
    desc = ""
    async for doc in records:
        user = bot.get_user(doc['user_id'])
        time = doc['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')
        desc += f"**{user}** - {time}\n"
    if not desc:
        desc = "No bumps recorded yet."
    embed = discord.Embed(title="📜 Bump History", description=desc, color=0x00ffcc)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="userbumps", description="See how many times a user has bumped.")
async def user_bumps(interaction: discord.Interaction, user: discord.User):
    count = bump_col.count_documents({"guild_id": interaction.guild_id, "user_id": user.id})
    await interaction.response.send_message(f"{user.mention} has bumped **{count}** times.", ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    config = config_col.find_one({"_id": guild_id})
    scan_channel_id = config.get("scan_channel") if config else None

    if message.channel.id != scan_channel_id:
        return

    if message.embeds and "Bump done!" in message.embeds[0].description:
        now = datetime.datetime.utcnow()
        bump_col.insert_one({
            "guild_id": guild_id,
            "user_id": message.interaction.user.id if message.interaction else None,
            "timestamp": now
        })

        reminders[guild_id] = {
            "next_bump": now + datetime.timedelta(hours=2),
            "user_id": message.interaction.user.id if message.interaction else None
        }

        log_id = config.get("log_channel") if config else None
        if log_id:
            log_channel = bot.get_channel(log_id)
            if log_channel:
                await log_channel.send(f"✅ Bump by {message.interaction.user.mention if message.interaction else 'Unknown'} — Next bump in 2 hours.")

        await asyncio.sleep(7200)

        updated = reminders.get(guild_id)
        if updated and updated["next_bump"] <= datetime.datetime.utcnow():
            role_id = config.get("ping_role")
            if role_id:
                ping = f"<@&{role_id}>"
            else:
                ping = "@here"
            if log_channel:
                await log_channel.send(f"🔔 {ping} Time to bump the server again!")

@tasks.loop(seconds=30)
async def scanner_loop():
    for guild in bot.guilds:
        config = config_col.find_one({"_id": guild.id})
        if not config:
            continue

        interval = config.get("scanner_interval", 60)
        scan_channel = bot.get_channel(config.get("scan_channel", 0))
        if not scan_channel:
            continue

        async for message in scan_channel.history(limit=10):
            if message.embeds and "Bump done!" in message.embeds[0].description:
                return  # Already handled in on_message
        await asyncio.sleep(interval)

bot.run(TOKEN)
