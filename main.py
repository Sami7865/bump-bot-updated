import os
import discord
import asyncio
import datetime
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
from flask import Flask
import threading

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

TOKEN = os.environ.get("TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")

cluster = MongoClient(MONGO_URL)
db = cluster["bumpDB"]
config_col = db["config"]
bump_col = db["bumps"]

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")
    check_bumps.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Detect Disboard bump
    if message.channel and (message.interaction or message.embeds):
        embed = message.embeds[0] if message.embeds else None
        if embed and "Bump done" in embed.description:
            guild_id = message.guild.id
            user_id = message.interaction.user.id if message.interaction else message.mentions[0].id if message.mentions else None

            if not user_id:
                return

            now = datetime.datetime.utcnow()

            config = config_col.find_one({"guild_id": guild_id})
            if not config:
                return

            config_col.update_one(
                {"guild_id": guild_id},
                {"$set": {"last_bump": now, "bumper_id": user_id}},
                upsert=True
            )

            bump_col.insert_one({
                "guild_id": guild_id,
                "user_id": user_id,
                "timestamp": now
            })

            log_channel = bot.get_channel(config.get("log_channel"))
            if log_channel:
                next_time = now + datetime.timedelta(hours=2)
                await log_channel.send(
                    embed=discord.Embed(
                        title="✅ Bump Logged",
                        description=f"<@{user_id}> just bumped the server.\nNext bump <t:{int(next_time.timestamp())}:R>",
                        color=discord.Color.green()
                    )
                )

@tasks.loop(minutes=1)
async def check_bumps():
    now = datetime.datetime.utcnow()
    for config in config_col.find():
        last_bump = config.get("last_bump")
        if not last_bump:
            continue

        bumper_id = config.get("bumper_id")
        if not bumper_id:
            continue

        time_diff = now - last_bump
        if time_diff >= datetime.timedelta(hours=2):
            guild = bot.get_guild(config["guild_id"])
            if guild:
                log_channel = guild.get_channel(config["log_channel"])
                ping_role = guild.get_role(config["ping_role"])
                if log_channel and ping_role:
                    await log_channel.send(
                        f"{ping_role.mention} <@{bumper_id}> it's time to bump the server again!",
                        embed=discord.Embed(
                            title="⏰ Bump Reminder",
                            description="It’s been 2 hours since the last bump.",
                            color=discord.Color.orange()
                        )
                    )
                    # Clear reminder to avoid repeat pings
                    config_col.update_one(
                        {"guild_id": guild.id},
                        {"$unset": {"last_bump": "", "bumper_id": ""}}
                    )

@tree.command(name="setlogchannel", description="Set the channel where bump logs will be sent.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_col.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {"log_channel": channel.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role that should be pinged for bump reminders.")
@app_commands.checks.has_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    config_col.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {"ping_role": role.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(name="bumpstatus", description="Check the time remaining until the next bump.")
async def bump_status(interaction: discord.Interaction):
    config = config_col.find_one({"guild_id": interaction.guild.id})
    if config and "last_bump" in config:
        last_bump = config["last_bump"]
        next_bump = last_bump + datetime.timedelta(hours=2)
        now = datetime.datetime.utcnow()
        if next_bump > now:
            remaining = next_bump - now
            minutes = int(remaining.total_seconds() // 60)
            await interaction.response.send_message(f"⏳ Next bump in {minutes} minutes (<t:{int(next_bump.timestamp())}:R>)", ephemeral=True)
        else:
            await interaction.response.send_message("✅ It's time to bump now!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ No bump has been recorded yet.", ephemeral=True)

@tree.command(name="bumphistory", description="Show bump history for the server.")
async def bump_history(interaction: discord.Interaction):
    records = bump_col.find({"guild_id": interaction.guild.id}).sort("timestamp", -1).limit(10)
    if await records.count() == 0:
        await interaction.response.send_message("No bumps have been recorded yet.", ephemeral=True)
        return

    embed = discord.Embed(title="📜 Recent Bumps", color=discord.Color.blue())
    for record in records:
        user_id = record["user_id"]
        timestamp = record["timestamp"]
        embed.add_field(name=f"<@{user_id}>", value=f"<t:{int(timestamp.timestamp())}:R>", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="userbumps", description="Show how many bumps a user has made.")
async def user_bumps(interaction: discord.Interaction, user: discord.Member):
    count = bump_col.count_documents({"guild_id": interaction.guild.id, "user_id": user.id})
    await interaction.response.send_message(f"👤 {user.mention} has bumped the server {count} times.", ephemeral=True)

# Flask server for UptimeRobot ping
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run).start()

bot.run(TOKEN)
