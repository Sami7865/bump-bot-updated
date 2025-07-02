import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from dotenv import load_dotenv
from pymongo import MongoClient
import datetime
from flask import Flask
import threading

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Mongo setup
mongo = MongoClient(MONGO_URL)
db = mongo["bump_bot"]
config = db["config"]
bumps = db["bumps"]
user_bumps = db["user_bumps"]

# Flask for uptime
app = Flask("")

@app.route("/")
def home():
    return "Bot is running!"

def run():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run).start()

# Globals
REMINDER_INTERVAL = 120  # in minutes

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")
    bump_reminder_loop.start()

@bot.event
async def on_message(message):
    if message.author.id != 302050872383242240:  # Disboard bot ID
        return

    if message.embeds:
        embed = message.embeds[0]
        if embed.description and "Bump done!" in embed.description:
            guild_id = message.guild.id
            user = message.interaction.user if message.interaction else message.mentions[0] if message.mentions else None
            if user:
                now = datetime.datetime.utcnow()

                # Save bump
                bumps.update_one(
                    {"guild_id": str(guild_id)},
                    {"$set": {"last_bump_time": now, "last_bumper_id": str(user.id)}},
                    upsert=True
                )

                user_bumps.update_one(
                    {"guild_id": str(guild_id), "user_id": str(user.id)},
                    {"$push": {"timestamps": now}},
                    upsert=True
                )

                # Log channel
                conf = config.find_one({"guild_id": str(guild_id)})
                if conf:
                    log_channel = bot.get_channel(int(conf["log_channel_id"]))
                    if log_channel:
                        next_bump = now + datetime.timedelta(minutes=REMINDER_INTERVAL)
                        await log_channel.send(
                            embed=discord.Embed(
                                title="✅ Bump Recorded",
                                description=f"{user.mention} bumped the server.\nNext bump at <t:{int(next_bump.timestamp())}:t>.",
                                color=discord.Color.green()
                            )
                        )
    await bot.process_commands(message)

# Background reminder task
@tasks.loop(minutes=1)
async def bump_reminder_loop():
    now = datetime.datetime.utcnow()
    for doc in bumps.find():
        last_bump = doc.get("last_bump_time")
        guild_id = int(doc["guild_id"])
        user_id = int(doc["last_bumper_id"])
        conf = config.find_one({"guild_id": str(guild_id)})
        if not (last_bump and conf):
            continue

        if (now - last_bump).total_seconds() > REMINDER_INTERVAL * 60:
            guild = bot.get_guild(guild_id)
            user = guild.get_member(user_id) if guild else None
            channel = bot.get_channel(int(conf["log_channel_id"]))
            role = guild.get_role(int(conf["ping_role_id"])) if guild else None

            if channel and role:
                await channel.send(
                    f"{role.mention} ⏰ {user.mention if user else 'User'} please bump the server again!",
                    embed=discord.Embed(
                        description="2 hours have passed since the last bump.",
                        color=discord.Color.orange()
                    )
                )
                # Reset bump time so we don’t spam
                bumps.update_one({"guild_id": str(guild_id)}, {"$set": {"last_bump_time": now}})

# Slash commands
@tree.command(name="setlogchannel", description="Set the channel where bump logs will be sent.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"log_channel_id": str(channel.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role to ping after 2 hours of last bump.")
@app_commands.checks.has_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    config.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"ping_role_id": str(role.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

@tree.command(name="bumpstatus", description="Show the time left for next bump.")
async def bump_status(interaction: discord.Interaction):
    doc = bumps.find_one({"guild_id": str(interaction.guild.id)})
    if not doc or "last_bump_time" not in doc:
        await interaction.response.send_message("❌ No bump recorded yet.", ephemeral=True)
        return

    last_bump = doc["last_bump_time"]
    next_bump = last_bump + datetime.timedelta(minutes=REMINDER_INTERVAL)
    remaining = next_bump - datetime.datetime.utcnow()
    seconds = int(remaining.total_seconds())

    if seconds <= 0:
        msg = "✅ You can bump now!"
    else:
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        msg = f"⏳ Next bump in {hours}h {minutes}m {sec}s (<t:{int(next_bump.timestamp())}:R>)"

    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="bumphistory", description="Show recent bump history.")
async def bump_history(interaction: discord.Interaction):
    logs = user_bumps.find({"guild_id": str(interaction.guild.id)})
    desc = ""
    for entry in logs:
        user_id = int(entry["user_id"])
        timestamps = entry["timestamps"][-3:]
        user = interaction.guild.get_member(user_id)
        name = user.mention if user else f"<@{user_id}>"
        formatted = "\n".join(f"<t:{int(ts.timestamp())}:f>" for ts in timestamps)
        desc += f"**{name}**\n{formatted}\n\n"

    if not desc:
        desc = "No bumps recorded yet."

    embed = discord.Embed(title="📜 Bump History", description=desc, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="userbumps", description="Show bump timestamps for a user.")
async def user_bump_log(interaction: discord.Interaction, user: discord.User):
    record = user_bumps.find_one({"guild_id": str(interaction.guild.id), "user_id": str(user.id)})
    if not record:
        await interaction.response.send_message("No bumps recorded for that user.", ephemeral=True)
        return

    desc = "\n".join(f"<t:{int(ts.timestamp())}:f>" for ts in record["timestamps"][-10:])
    embed = discord.Embed(title=f"📘 Bumps by {user}", description=desc, color=discord.Color.teal())
    await interaction.response.send_message(embed=embed, ephemeral=True)

bot.run(TOKEN)
