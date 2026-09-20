import os
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from aiohttp import web


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

if not TOKEN:
    raise RuntimeError("❌ Thiếu DISCORD_TOKEN")

if not MONGODB_URI:
    raise RuntimeError("❌ Thiếu MONGODB_URI")


# Render cung cấp PORT tự động
PORT = int(os.getenv("PORT", "10000"))


# =========================================================
# DATABASE
# =========================================================

mongo = AsyncIOMotorClient(MONGODB_URI)

db = mongo["account_manager"]

accounts = db["accounts"]
settings = db["settings"]


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# HELPERS
# =========================================================

def utc_now():
    return datetime.now(timezone.utc)


async def get_settings(guild_id: int):
    return await settings.find_one({
        "guild_id": guild_id
    })


async def get_account(
    guild_id: int,
    username: str
):
    return await accounts.find_one({
        "guild_id": guild_id,
        "username": username
    })


async def is_admin(
    interaction: discord.Interaction
):
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.guild_permissions.manage_guild
    )


# =========================================================
# WEB SERVER
# =========================================================

async def health(request):
    return web.Response(
        text="Discord bot is online."
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"🌐 Web server listening on port {PORT}"
    )


# =========================================================
# ACCOUNT EMBED
# =========================================================

async def build_account_embed(
    guild_id: int
):

    data = await accounts.find({
        "guild_id": guild_id
    }).sort(
        "created_at",
        1
    ).to_list(
        length=500
    )

    embed = discord.Embed(
        title="📋 DANH SÁCH ACC",
        color=discord.Color.blurple(),
        timestamp=utc_now()
    )

    if not data:

        embed.description = (
            "Chưa có tài khoản nào.\n\n"
            "Nhấn **➕ Thêm ACC** để thêm."
        )

        return embed

    lines = []

    for acc in data:

        username = acc.get(
            "username",
            "Unknown"
        )

        note = acc.get(
            "note",
            "Không có ghi chú"
        )

        status = acc.get(
            "status",
            "Chưa xử lý"
        )

        status_icon = {
            "Chưa xử lý": "⚪",
            "Đang cày": "🟡",
            "Hoàn thành": "🟢",
            "Tạm dừng": "⏸️",
            "Đã hủy": "🔴"
        }.get(
            status,
            "⚪"
        )

        lines.append(
            f"👤 **{username}**\n"
            f"   📝 {note}\n"
            f"   {status_icon} `{status}`"
        )

    description = "\n\n".join(lines)

    if len(description) > 4000:
        description = (
            description[:3950]
            + "\n..."
        )

    embed.description = description

    embed.set_footer(
        text=f"Tổng ACC: {len(data)}"
    )

    return embed


# =========================================================
# REFRESH PANEL
# =========================================================

async def refresh_account_panel(
    guild: discord.Guild
):

    config = await get_settings(
        guild.id
    )

    if not config:
        return

    channel_id = config.get(
        "channel_id"
    )

    message_id = config.get(
        "message_id"
    )

    if not channel_id:
        return

    channel = guild.get_channel(
        channel_id
    )

    if not channel:
        return

    embed = await build_account_embed(
        guild.id
    )

    # -----------------------------------------
    # EDIT MESSAGE
    # -----------------------------------------

    if message_id:

        try:

            message = await channel.fetch_message(
                message_id
            )

            await message.edit(
                embed=embed,
                view=AccountPanelView()
            )

            return

        except discord.NotFound:
            pass

        except discord.Forbidden:
            print(
                "❌ Bot không có quyền sửa message."
            )

            return

    # -----------------------------------------
    # CREATE MESSAGE
    # -----------------------------------------

    message = await channel.send(
        embed=embed,
        view=AccountPanelView()
    )

    await settings.update_one(
        {
            "guild_id": guild.id
        },
        {
            "$set": {
                "channel_id": channel.id,
                "message_id": message.id
            }
        },
        upsert=True
    )


# =========================================================
# ADD ACCOUNT MODAL
# =========================================================

class AddAccountModal(
    discord.ui.Modal,
    title="➕ Thêm tài khoản"
):

    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Nhập username...",
        required=True,
        max_length=100
    )

    password = discord.ui.TextInput(
        label="Mật khẩu",
        placeholder="Nhập mật khẩu...",
        required=True,
        max_length=200
    )

    note = discord.ui.TextInput(
        label="Ghi chú",
        placeholder="Ví dụ: Cày rank...",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await is_admin(interaction):

            await interaction.response.send_message(
                "❌ Bạn không có quyền thêm ACC.",
                ephemeral=True
            )

            return

        username = self.username.value.strip()

        existing = await get_account(
            interaction.guild.id,
            username
        )

        if existing:

            await interaction.response.send_message(
                "❌ Username này đã tồn tại.",
                ephemeral=True
            )

            return

        document = {

            "guild_id": interaction.guild.id,

            "username": username,

            "password": self.password.value,

            "note": (
                self.note.value.strip()
                or "Không có ghi chú"
            ),

            "status": "Chưa xử lý",

            "created_at": utc_now(),

            "updated_at": utc_now()
        }

        await accounts.insert_one(
            document
        )

        await refresh_account_panel(
            interaction.guild
        )

        await interaction.response.send_message(
            f"✅ Đã thêm ACC `{username}`.",
            ephemeral=True
        )


# =========================================================
# EDIT NOTE MODAL
# =========================================================

class EditNoteModal(
    discord.ui.Modal,
    title="📝 Sửa ghi chú"
):

    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Username của ACC",
        required=True
    )

    note = discord.ui.TextInput(
        label="Ghi chú mới",
        placeholder="Nhập ghi chú...",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await is_admin(interaction):

            await interaction.response.send_message(
                "❌ Bạn không có quyền sửa ACC.",
                ephemeral=True
            )

            return

        username = self.username.value.strip()

        result = await accounts.update_one(

            {
                "guild_id": interaction.guild.id,
                "username": username
            },

            {
                "$set": {
                    "note": self.note.value.strip(),
                    "updated_at": utc_now()
                }
            }
        )

        if result.matched_count == 0:

            await interaction.response.send_message(
                "❌ Không tìm thấy ACC.",
                ephemeral=True
            )

            return

        await refresh_account_panel(
            interaction.guild
        )

        await interaction.response.send_message(
            f"✅ Đã cập nhật ghi chú `{username}`.",
            ephemeral=True
        )


# =========================================================
# DELETE ACCOUNT MODAL
# =========================================================

class DeleteAccountModal(
    discord.ui.Modal,
    title="🗑️ Xóa tài khoản"
):

    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Username cần xóa",
        required=True
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await is_admin(interaction):

            await interaction.response.send_message(
                "❌ Bạn không có quyền xóa ACC.",
                ephemeral=True
            )

            return

        username = self.username.value.strip()

        result = await accounts.delete_one({

            "guild_id": interaction.guild.id,

            "username": username

        })

        if result.deleted_count == 0:

            await interaction.response.send_message(
                "❌ Không tìm thấy ACC.",
                ephemeral=True
            )

            return

        await refresh_account_panel(
            interaction.guild
        )

        await interaction.response.send_message(
            f"🗑️ Đã xóa `{username}`.",
            ephemeral=True
        )


# =========================================================
# GET ACCOUNT MODAL
# =========================================================

class GetAccountModal(
    discord.ui.Modal,
    title="🔐 Lấy thông tin ACC"
):

    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Nhập username...",
        required=True
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await is_admin(interaction):

            await interaction.response.send_message(
                "❌ Bạn không có quyền xem thông tin đăng nhập.",
                ephemeral=True
            )

            return

        username = self.username.value.strip()

        acc = await get_account(
            interaction.guild.id,
            username
        )

        if not acc:

            await interaction.response.send_message(
                "❌ Không tìm thấy ACC.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="🔐 THÔNG TIN ACC",
            color=discord.Color.green()
        )

        embed.add_field(
            name="👤 Username",
            value=f"`{acc['username']}`",
            inline=False
        )

        embed.add_field(
            name="🔑 Password",
            value=(
                f"||`{acc['password']}`||"
            ),
            inline=False
        )

        embed.add_field(
            name="📝 Ghi chú",
            value=acc.get(
                "note",
                "Không có ghi chú"
            ),
            inline=False
        )

        embed.add_field(
            name="📊 Trạng thái",
            value=acc.get(
                "status",
                "Chưa xử lý"
            ),
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


# =========================================================
# ACCOUNT PANEL
# =========================================================

class AccountPanelView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )


    @discord.ui.button(
        label="Thêm ACC",
        emoji="➕",
        style=discord.ButtonStyle.success,
        custom_id="account_add"
    )
    async def add_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await is_admin(interaction):

            await interaction.response.send_message(
                "❌ Bạn không có quyền.",
                ephemeral=True
            )

            return

        await interaction.response.send_modal(
            AddAccountModal()
        )


    @discord.ui.button(
        label="Sửa ghi chú",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        custom_id="account_note"
    )
    async def note_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            EditNoteModal()
        )


    @discord.ui.button(
        label="Lấy TK/MK",
        emoji="🔐",
        style=discord.ButtonStyle.secondary,
        custom_id="account_get"
    )
    async def get_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            GetAccountModal()
        )


    @discord.ui.button(
        label="Xóa ACC",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="account_delete"
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_modal(
            DeleteAccountModal()
        )


    @discord.ui.button(
        label="Làm mới",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="account_refresh"
    )
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await refresh_account_panel(
            interaction.guild
        )

        await interaction.response.send_message(
            "🔄 Đã làm mới bảng ACC.",
            ephemeral=True
        )


# =========================================================
# /setup
# =========================================================

@bot.tree.command(
    name="setup",
    description="Chọn kênh hiển thị bảng ACC"
)
@app_commands.describe(
    channel="Kênh Discord dùng để quản lý ACC"
)
async def setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    if not await is_admin(interaction):

        await interaction.response.send_message(
            "❌ Bạn không có quyền.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    await settings.update_one(

        {
            "guild_id": interaction.guild.id
        },

        {
            "$set": {
                "channel_id": channel.id,
                "message_id": None
            }
        },

        upsert=True
    )

    await refresh_account_panel(
        interaction.guild
    )

    await interaction.followup.send(
        f"✅ Đã tạo bảng ACC tại {channel.mention}.",
        ephemeral=True
    )


# =========================================================
# /acc_them
# =========================================================

@bot.tree.command(
    name="acc_them",
    description="Thêm tài khoản"
)
async def acc_them(
    interaction: discord.Interaction
):

    if not await is_admin(interaction):

        await interaction.response.send_message(
            "❌ Bạn không có quyền.",
            ephemeral=True
        )

        return

    await interaction.response.send_modal(
        AddAccountModal()
    )


# =========================================================
# /acc_lay
# =========================================================

@bot.tree.command(
    name="acc_lay",
    description="Lấy username và password"
)
@app_commands.describe(
    username="Tên đăng nhập của ACC"
)
async def acc_lay(
    interaction: discord.Interaction,
    username: str
):

    if not await is_admin(interaction):

        await interaction.response.send_message(
            "❌ Bạn không có quyền xem thông tin đăng nhập.",
            ephemeral=True
        )

        return

    acc = await get_account(
        interaction.guild.id,
        username
    )

    if not acc:

        await interaction.response.send_message(
            "❌ Không tìm thấy ACC.",
            ephemeral=True
        )

        return

    embed = discord.Embed(
        title="🔐 THÔNG TIN ACC",
        color=discord.Color.green()
    )

    embed.add_field(
        name="👤 Username",
        value=f"`{acc['username']}`",
        inline=False
    )

    embed.add_field(
        name="🔑 Password",
        value=(
            f"||`{acc['password']}`||"
        ),
        inline=False
    )

    embed.add_field(
        name="📝 Ghi chú",
        value=acc.get(
            "note",
            "Không có ghi chú"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Trạng thái",
        value=acc.get(
            "status",
            "Chưa xử lý"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /acc_status
# =========================================================

@bot.tree.command(
    name="acc_status",
    description="Đổi trạng thái ACC"
)
@app_commands.describe(
    username="Tên đăng nhập",
    status="Trạng thái mới"
)
@app_commands.choices(
    status=[
        app_commands.Choice(
            name="Chưa xử lý",
            value="Chưa xử lý"
        ),
        app_commands.Choice(
            name="Đang cày",
            value="Đang cày"
        ),
        app_commands.Choice(
            name="Hoàn thành",
            value="Hoàn thành"
        ),
        app_commands.Choice(
            name="Tạm dừng",
            value="Tạm dừng"
        ),
        app_commands.Choice(
            name="Đã hủy",
            value="Đã hủy"
        )
    ]
)
async def acc_status(
    interaction: discord.Interaction,
    username: str,
    status: app_commands.Choice[str]
):

    if not await is_admin(interaction):

        await interaction.response.send_message(
            "❌ Bạn không có quyền.",
            ephemeral=True
        )

        return

    result = await accounts.update_one(

        {
            "guild_id": interaction.guild.id,
            "username": username
        },

        {
            "$set": {
                "status": status.value,
                "updated_at": utc_now()
            }
        }
    )

    if result.matched_count == 0:

        await interaction.response.send_message(
            "❌ Không tìm thấy ACC.",
            ephemeral=True
        )

        return

    await refresh_account_panel(
        interaction.guild
    )

    await interaction.response.send_message(
        f"✅ `{username}` → **{status.value}**",
        ephemeral=True
    )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():

    # Persistent buttons
    bot.add_view(
        AccountPanelView()
    )

    try:

        synced = await bot.tree.sync()

        print(
            f"🤖 Bot online: {bot.user}"
        )

        print(
            f"✅ Synced {len(synced)} slash commands"
        )

    except Exception as e:

        print(
            f"❌ Sync command lỗi: {e}"
        )


# =========================================================
# START
# =========================================================

async def main():

    # Chạy web server
    await start_web_server()

    # Chạy Discord bot
    await bot.start(TOKEN)


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass
