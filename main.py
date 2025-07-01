import os
import discord
from discord.ext import commands, tasks
from flask import Flask
import threading
from datetime import datetime, timedelta
from pymongo import MongoClient
import pytz

# Token from environment variable
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set.")

# Mongo URI from environment variable
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable not set.")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

client = commands.Bot(command_prefix='!', intents=intents)
tree = client.tree

db = MongoClient(MONGO_URI).bump_bot
reminder_data = {}

@client.event
async def on_ready():
    print(f'Bot is online as {client.user}')
    await tree.sync()

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.guild:
        return

    content_lower = message.content.lower()
    is_disboard_bump = "bump done" in content_lower or "bump successful" in content_lower

    # fallback if interaction is missing
    author = message.author
    if message.interaction:
        author = message.interaction.user

    if is_disboard_bump:
        data = db.configs.find_one({"guild_id": message.guild.id})
        if not data:
            return

        ping_role_id = data.get("ping_role")
        log_channel_id = data.get("log_channel")

        if not ping_role_id or not log_channel_id:
            return

        # Save user bump info
        db.bump_history.insert_one({
            "guild_id": message.guild.id,
            "user_id": author.id,
            "timestamp": datetime.utcnow()
        })

        now = datetime.now(pytz.utc)
        reminder_data[message.guild.id] = {
            "user_id": author.id,
            "next_bump": now + timedelta(hours=2)
        }

        log_channel = message.guild.get_channel(log_channel_id)
        if log_channel:
            embed = discord.Embed(
                title="✅ Server Bumped!",
                description=f"{author.mention} just bumped the server.\n\nNext bump available <t:{int(reminder_data[message.guild.id]['next_bump'].timestamp())}:R>.",
                color=discord.Color.green()
            )
            await log_channel.send(embed=embed)

@tasks.loop(minutes=1)
async def check_reminders():
    for guild_id, data in list(reminder_data.items()):
        if datetime.now(pytz.utc) >= data["next_bump"]:
            guild = client.get_guild(guild_id)
            if not guild:
                continue

            config = db.configs.find_one({"guild_id": guild_id})
            if not config:
                continue

            role = guild.get_role(config["ping_role"])
            log_channel = guild.get_channel(config["log_channel"])
            user = guild.get_member(data["user_id"])

            if role and log_channel and user:
                embed = discord.Embed(
                    title="⏰ Time to Bump Again!",
                    description=f"{user.mention}, it's time to bump the server again with `/bump`!",
                    color=discord.Color.orange()
                )
                await log_channel.send(content=role.mention, embed=embed)

            # Remove reminder
            del reminder_data[guild_id]

check_reminders.start()

# SLASH COMMANDS

@tree.command(name="setlogchannel", description="Set the bump log channel")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    db.configs.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {"log_channel": channel.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role to ping for bump reminders")
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    db.configs.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {"ping_role": role.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(name="bumpstatus", description="Check time remaining until next bump")
async def bump_status(interaction: discord.Interaction):
    data = reminder_data.get(interaction.guild.id)
    if data:
        timestamp = int(data["next_bump"].timestamp())
        await interaction.response.send_message(
            f"⏳ Next bump available <t:{timestamp}:R>", ephemeral=True
        )
    else:
        await interaction.response.send_message("✅ No bump reminder active right now.", ephemeral=True)

@tree.command(name="bumphistory", description="Show recent bump history")
async def bump_history(interaction: discord.Interaction):
    bumps = db.bump_history.find({"guild_id": interaction.guild.id}).sort("timestamp", -1).limit(10)
    embed = discord.Embed(title="📜 Bump History", color=discord.Color.blurple())
    for bump in bumps:
        user = interaction.guild.get_member(bump["user_id"])
        if user:
            time_str = bump["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
            embed.add_field(name=user.name, value=time_str, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="userbumps", description="Show bump history of a specific user")
async def user_bumps(interaction: discord.Interaction, user: discord.Member):
    bumps = db.bump_history.find({
        "guild_id": interaction.guild.id,
        "user_id": user.id
    }).sort("timestamp", -1).limit(10)

    embed = discord.Embed(
        title=f"📈 {user.display_name}'s Bump History",
        color=discord.Color.purple()
    )
    for bump in bumps:
        time_str = bump["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        embed.add_field(name="•", value=time_str, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# Flask app for uptime monitoring
app = Flask('')

@app.route('/')
def home():
    return "Bump bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run).start()

# Run the bot
client.run(TOKEN)
