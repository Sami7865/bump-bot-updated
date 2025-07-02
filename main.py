import discord
from discord.ext import tasks
from discord import app_commands
import datetime
import pymongo
import os
from flask import Flask
import threading

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

TOKEN = os.environ['DISCORD_TOKEN']
MONGO_URL = os.environ['MONGO_URL']
mongo_client = pymongo.MongoClient(MONGO_URL)
db = mongo_client['bump_database']
bump_collection = db['bumps']
config_collection = db['config']

client.activity = discord.Activity(type=discord.ActivityType.listening, name="/bump")

# HTTP server for uptime
app = Flask('')
@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run).start()

# Store scheduled reminders
scheduled_reminders = {}

def get_time_diff_string(timestamp):
    now = datetime.datetime.now(datetime.UTC)
    remaining = timestamp - now
    if remaining.total_seconds() <= 0:
        return "Now"
    mins, secs = divmod(int(remaining.total_seconds()), 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"

async def log_to_channel(guild: discord.Guild, content: str):
    config = config_collection.find_one({"_id": str(guild.id)})
    if config and "log_channel" in config:
        log_channel = guild.get_channel(config["log_channel"])
        if log_channel:
            await log_channel.send(content)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel and (message.interaction or message.embeds):
        if message.embeds:
            embed = message.embeds[0]
            if "Bump done" in embed.description or "Bump done" in embed.title:
                now = datetime.datetime.now(datetime.UTC)
                guild_id = str(message.guild.id)
                bumper = message.interaction.user if message.interaction else message.author

                bump_collection.insert_one({
                    "guild_id": guild_id,
                    "user_id": str(bumper.id),
                    "timestamp": now
                })

                scheduled_reminders[guild_id] = {
                    "user_id": bumper.id,
                    "time": now + datetime.timedelta(hours=2)
                }

                await log_to_channel(message.guild, f"🟢 **{bumper.mention}** bumped the server. Next bump in 2 hours.")

@tasks.loop(minutes=1)
async def reminder_task():
    now = datetime.datetime.now(datetime.UTC)
    for guild_id, data in list(scheduled_reminders.items()):
        if now >= data["time"]:
            guild = client.get_guild(int(guild_id))
            if guild:
                config = config_collection.find_one({"_id": guild_id})
                if config and "ping_role" in config and "log_channel" in config:
                    role = guild.get_role(config["ping_role"])
                    channel = guild.get_channel(config["log_channel"])
                    user = guild.get_member(data["user_id"])
                    if role and channel and user:
                        await channel.send(f"🔔 {user.mention} it's time to **/bump** again! {role.mention}")
                        await log_to_channel(guild, f"🔔 Reminder sent to {user.mention} to bump again.")
            del scheduled_reminders[guild_id]

@tree.command(name="setlogchannel", description="Set the log channel for bumps and reminders")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_collection.update_one(
        {"_id": str(interaction.guild.id)},
        {"$set": {"log_channel": channel.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role to ping for bump reminders")
@app_commands.checks.has_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    config_collection.update_one(
        {"_id": str(interaction.guild.id)},
        {"$set": {"ping_role": role.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(name="bumpstatus", description="Check when the next bump is due")
async def bump_status(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    if guild_id in scheduled_reminders:
        timestamp = scheduled_reminders[guild_id]["time"]
        remaining = get_time_diff_string(timestamp)
        await interaction.response.send_message(f"⏳ Next bump available in: **{remaining}**", ephemeral=True)
    else:
        await interaction.response.send_message("✅ No active bump timer. You can bump now!", ephemeral=True)

@tree.command(name="resetbump", description="Reset the bump timer manually")
@app_commands.checks.has_permissions(administrator=True)
async def reset_bump(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    if guild_id in scheduled_reminders:
        del scheduled_reminders[guild_id]
        await interaction.response.send_message("🔁 Bump timer has been reset.", ephemeral=True)
        await log_to_channel(interaction.guild, f"🔁 Bump timer manually reset by {interaction.user.mention}")
    else:
        await interaction.response.send_message("ℹ️ No active bump timer to reset.", ephemeral=True)

@tree.command(name="bumphistory", description="See the recent bump history")
async def bump_history(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    records = bump_collection.find({"guild_id": guild_id}).sort("timestamp", -1).limit(10)
    records = list(records)
    if not records:
        await interaction.response.send_message("📭 No bump history found.", ephemeral=True)
        return

    description = ""
    for record in records:
        user = interaction.guild.get_member(int(record["user_id"]))
        timestamp = record["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        description += f"• **{user.mention if user else 'Unknown'}** at `{timestamp}`\n"

    embed = discord.Embed(title="📜 Recent Bumps", description=description, color=0x00ff99)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="userbumps", description="Check how many times a user has bumped")
async def user_bumps(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild.id)
    count = bump_collection.count_documents({"guild_id": guild_id, "user_id": str(user.id)})
    await interaction.response.send_message(f"👤 {user.mention} has bumped **{count}** time(s).", ephemeral=True)

client.run(TOKEN)
