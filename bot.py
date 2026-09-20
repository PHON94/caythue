import os
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# =========================================================
# ENV
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN trong Environment Variables.")

if not MONGODB_URI:
    raise RuntimeError("Thiếu MONGODB_URI trong Environment Variables.")


# =========================================================
# MONGODB
# =========================================================

mongo = AsyncIOMotorClient(MONGODB_URI)
db = mongo["account_manager"]

accounts_col = db["accounts"]
settings_col = db["settings"]


# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# Tránh sync command / add view nhiều lần
_setup_done = False


# =========================================================
# HELPERS
# =========================================================

def utc_now():
    return datetime.now(timezone.utc)


def is_admin(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.user.guild_permissions.administrator
        if interaction.guild
        else False
    )


async def get_settings():
    return await settings_col.find_one({"_id": "main"})


async def get_account(username: str):
    return await accounts_col.find_one({
        "username": username.strip()
    })


def status_icon(status: str) -> str:
    icons = {
        "Chưa xử lý": "🟡",
        "Đang cày": "🔵",
        "Hoàn thành": "🟢",
        "Tạm dừng": "🟠",
        "Đã hủy": "🔴",
    }
    return icons.get(status, "⚪")


async def build_account_embed() -> discord.Embed:
    accounts = await accounts_col.find().sort("createdAt", 1).to_list(length=None)

    embed = discord.Embed(
        title="📋 DANH SÁCH ACC CÀY THUÊ",
        color=discord.Color.blurple(),
        timestamp=utc_now()
    )

    if not accounts:
        embed.description = "Chưa có tài khoản nào được thêm."
    else:
        # Mỗi ACC nằm trên một hàng ngang:
        # STT | Tên đăng nhập | Ghi chú | Trạng thái
        header = (
            "```text\n"
            "STT  | TÊN ĐĂNG NHẬP       | GHI CHÚ              | TRẠNG THÁI\n"
            "-----|---------------------|----------------------|-------------"
        )

        rows = []

        for index, acc in enumerate(accounts, start=1):
            username = str(acc.get("username", "Không rõ"))
            note = str(acc.get("note", "")).strip() or "Không có"
            status = str(acc.get("status", "Chưa xử lý"))

            # Giữ bảng gọn trên Discord.
            username = username.replace("`", "'")[:19]
            note = note.replace("`", "'").replace("\n", " ")[:20]
            status_text = f"{status_icon(status)} {status}"[:21]

            rows.append(
                f"{index:<4} | "
                f"{username:<19} | "
                f"{note:<20} | "
                f"{status_text:<21}"
            )

        # Discord giới hạn độ dài embed, nên chia thành nhiều block
        # nếu có nhiều ACC.
        current = header

        for row in rows:
            if len(current) + len(row) + 10 > 3900:
                current += "\n```"
                embed.add_field(
                    name="📋 Danh sách ACC",
                    value=current,
                    inline=False
                )
                current = header

            current += "\n" + row

        current += "\n```"

        embed.add_field(
            name="📋 Danh sách ACC",
            value=current,
            inline=False
        )

    embed.set_footer(
        text=f"Tổng ACC: {len(accounts)} • Dữ liệu lưu trên MongoDB"
    )

    return embed


# =========================================================
# REBUILD PANEL
# =========================================================
# Mỗi khi dữ liệu thay đổi:
# 1. Xóa message bảng cũ
# 2. Tạo message bảng mới
# 3. Gắn lại toàn bộ nút
#
# Dữ liệu MongoDB không bị xóa.
# =========================================================

async def rebuild_account_panel() -> bool:
    settings = await get_settings()

    if not settings:
        return False

    channel_id = settings.get("channel_id")
    old_message_id = settings.get("message_id")

    if not channel_id:
        return False

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return False

    # Xóa bảng cũ
    if old_message_id:
        try:
            old_message = await channel.fetch_message(old_message_id)
            await old_message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            print("⚠️ Bot không có quyền xóa message bảng cũ.")
        except discord.HTTPException as e:
            print(f"⚠️ Không thể xóa bảng cũ: {e}")

    # Tạo bảng mới
    embed = await build_account_embed()

    new_message = await channel.send(
        embed=embed,
        view=AccountPanelView()
    )

    # Lưu ID bảng mới
    await settings_col.update_one(
        {"_id": "main"},
        {
            "$set": {
                "channel_id": channel.id,
                "message_id": new_message.id,
                "updatedAt": utc_now()
            }
        },
        upsert=True
    )

    return True


# =========================================================
# MODAL: ADD ACCOUNT
# =========================================================

class AddAccountModal(discord.ui.Modal, title="➕ Thêm ACC"):
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
        placeholder="Ví dụ: Cày level / cày bone...",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        username = self.username.value.strip()

        existing = await get_account(username)

        if existing:
            await interaction.response.send_message(
                f"❌ ACC `{username}` đã tồn tại.",
                ephemeral=True
            )
            return

        now = utc_now()

        await accounts_col.insert_one({
            "username": username,
            "password": self.password.value,
            "note": self.note.value.strip(),
            "status": "Chưa xử lý",
            "createdAt": now,
            "updatedAt": now
        })

        await interaction.response.send_message(
            f"✅ Đã thêm ACC `{username}`.",
            ephemeral=True
        )

        try:
            await rebuild_account_panel()
        except Exception as e:
            print(f"❌ Lỗi rebuild bảng sau khi thêm ACC: {e}")


# =========================================================
# MODAL: EDIT NOTE
# =========================================================

class EditNoteModal(discord.ui.Modal, title="📝 Sửa ghi chú"):
    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Nhập username cần sửa...",
        required=True,
        max_length=100
    )

    note = discord.ui.TextInput(
        label="Ghi chú mới",
        placeholder="Nhập ghi chú mới...",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        username = self.username.value.strip()

        account = await get_account(username)

        if not account:
            await interaction.response.send_message(
                f"❌ Không tìm thấy ACC `{username}`.",
                ephemeral=True
            )
            return

        await accounts_col.update_one(
            {"_id": account["_id"]},
            {
                "$set": {
                    "note": self.note.value.strip(),
                    "updatedAt": utc_now()
                }
            }
        )

        await interaction.response.send_message(
            f"✅ Đã sửa ghi chú của `{username}`.",
            ephemeral=True
        )

        try:
            await rebuild_account_panel()
        except Exception as e:
            print(f"❌ Lỗi rebuild bảng sau khi sửa ghi chú: {e}")


# =========================================================
# MODAL: GET ACCOUNT
# =========================================================

class GetAccountModal(discord.ui.Modal, title="🔑 Lấy TK / MK"):
    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Nhập username...",
        required=True,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "❌ Chỉ Administrator mới được lấy TK/MK.",
                ephemeral=True
            )
            return

        username = self.username.value.strip()
        account = await get_account(username)

        if not account:
            await interaction.response.send_message(
                f"❌ Không tìm thấy ACC `{username}`.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔐 Thông tin ACC",
            color=discord.Color.green()
        )

        embed.add_field(
            name="👤 Tài khoản",
            value=f"`{account.get('username', '')}`",
            inline=False
        )

        embed.add_field(
            name="🔑 Mật khẩu",
            value=f"||{account.get('password', '')}||",
            inline=False
        )

        embed.add_field(
            name="📝 Ghi chú",
            value=account.get("note", "") or "Không có",
            inline=False
        )

        embed.add_field(
            name="📌 Trạng thái",
            value=account.get("status", "Chưa xử lý"),
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


# =========================================================
# MODAL: DELETE ACCOUNT
# =========================================================

class DeleteAccountModal(discord.ui.Modal, title="🗑 Xóa ACC"):
    username = discord.ui.TextInput(
        label="Tên đăng nhập",
        placeholder="Nhập username cần xóa...",
        required=True,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        username = self.username.value.strip()

        result = await accounts_col.delete_one({
            "username": username
        })

        if result.deleted_count == 0:
            await interaction.response.send_message(
                f"❌ Không tìm thấy ACC `{username}`.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🗑️ Đã xóa ACC `{username}`.",
            ephemeral=True
        )

        try:
            await rebuild_account_panel()
        except Exception as e:
            print(f"❌ Lỗi rebuild bảng sau khi xóa ACC: {e}")


# =========================================================
# PANEL VIEW
# =========================================================

class AccountPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Thêm ACC",
        emoji="➕",
        style=discord.ButtonStyle.success,
        custom_id="account_add"
    )
    async def add_account(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(AddAccountModal())

    @discord.ui.button(
        label="Sửa ghi chú",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        custom_id="account_edit_note"
    )
    async def edit_note(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(EditNoteModal())

    @discord.ui.button(
        label="Lấy TK/MK",
        emoji="🔑",
        style=discord.ButtonStyle.secondary,
        custom_id="account_get"
    )
    async def get_account_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "❌ Chỉ Administrator mới được sử dụng chức năng này.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(GetAccountModal())

    @discord.ui.button(
        label="Xóa ACC",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="account_delete"
    )
    async def delete_account(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "❌ Chỉ Administrator mới được xóa ACC.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(DeleteAccountModal())

    @discord.ui.button(
        label="Làm mới",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="account_refresh"
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            success = await rebuild_account_panel()

            if success:
                await interaction.followup.send(
                    "🔄 Đã xóa bảng cũ và tạo bảng mới.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ Chưa cấu hình kênh bảng ACC. Dùng `/setup` trước.",
                    ephemeral=True
                )

        except Exception as e:
            print(f"❌ Lỗi refresh bảng: {e}")

            await interaction.followup.send(
                "❌ Không thể tạo lại bảng. Kiểm tra quyền bot và Render Logs.",
                ephemeral=True
            )


# =========================================================
# /setup
# =========================================================

@bot.tree.command(
    name="setup",
    description="Thiết lập kênh hiển thị bảng ACC"
)
@app_commands.describe(
    channel="Kênh Discord dùng để hiển thị bảng ACC"
)
async def setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Chỉ Administrator mới được dùng lệnh này.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    await settings_col.update_one(
        {"_id": "main"},
        {
            "$set": {
                "channel_id": channel.id,
                "message_id": None,
                "updatedAt": utc_now()
            }
        },
        upsert=True
    )

    try:
        success = await rebuild_account_panel()

        if success:
            await interaction.followup.send(
                f"✅ Đã thiết lập bảng ACC tại {channel.mention}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ Không thể tạo bảng.",
                ephemeral=True
            )

    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Bot không có quyền gửi/xóa message trong kênh đó.",
            ephemeral=True
        )

    except Exception as e:
        print(f"❌ Lỗi /setup: {e}")

        await interaction.followup.send(
            "❌ Có lỗi khi tạo bảng. Kiểm tra Render Logs.",
            ephemeral=True
        )


# =========================================================
# /acc_them
# =========================================================

@bot.tree.command(
    name="acc_them",
    description="Thêm một ACC vào hệ thống"
)
async def acc_them(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Chỉ Administrator mới được thêm ACC.",
            ephemeral=True
        )
        return

    await interaction.response.send_modal(AddAccountModal())


# =========================================================
# /acc_lay
# =========================================================

@bot.tree.command(
    name="acc_lay",
    description="Lấy thông tin TK/MK của ACC"
)
@app_commands.describe(
    username="Tên đăng nhập của ACC"
)
async def acc_lay(
    interaction: discord.Interaction,
    username: str
):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Chỉ Administrator mới được dùng lệnh này.",
            ephemeral=True
        )
        return

    account = await get_account(username.strip())

    if not account:
        await interaction.response.send_message(
            f"❌ Không tìm thấy ACC `{username}`.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🔐 Thông tin ACC",
        color=discord.Color.green()
    )

    embed.add_field(
        name="👤 Tài khoản",
        value=f"`{account.get('username', '')}`",
        inline=False
    )

    embed.add_field(
        name="🔑 Mật khẩu",
        value=f"||{account.get('password', '')}||",
        inline=False
    )

    embed.add_field(
        name="📝 Ghi chú",
        value=account.get("note", "") or "Không có",
        inline=False
    )

    embed.add_field(
        name="📌 Trạng thái",
        value=account.get("status", "Chưa xử lý"),
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
    description="Thay đổi trạng thái ACC"
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
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Chỉ Administrator mới được đổi trạng thái.",
            ephemeral=True
        )
        return

    account = await get_account(username.strip())

    if not account:
        await interaction.response.send_message(
            f"❌ Không tìm thấy ACC `{username}`.",
            ephemeral=True
        )
        return

    await accounts_col.update_one(
        {"_id": account["_id"]},
        {
            "$set": {
                "status": status.value,
                "updatedAt": utc_now()
            }
        }
    )

    await interaction.response.send_message(
        f"✅ `{username}` → **{status.value}**",
        ephemeral=True
    )

    try:
        await rebuild_account_panel()
    except Exception as e:
        print(f"❌ Lỗi rebuild bảng sau khi đổi status: {e}")


# =========================================================
# COMMAND: RENDER URL
# =========================================================

@bot.tree.command(
    name="url",
    description="Lấy URL Web Service Render của bot"
)
async def render_url(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Bạn không có quyền dùng lệnh này.",
            ephemeral=True
        )
        return

    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if not render_url:
        await interaction.response.send_message(
            "❌ Không tìm thấy `RENDER_EXTERNAL_URL`.\n"
            "Hãy kiểm tra bot đang chạy trên Render Web Service.",
            ephemeral=True
        )
        return

    render_url = render_url.rstrip("/")

    await interaction.response.send_message(
        "🌐 **Render URL**\n"
        f"{render_url}\n\n"
        "❤️ **Health URL**\n"
        f"{render_url}/health",
        ephemeral=True
    )


# =========================================================
# WEB SERVER FOR RENDER
# =========================================================

async def health(request):
    return web.Response(
        text="CayThue Discord Bot is running."
    )


async def start_web_server():
    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(f"🌐 Web server listening on port {PORT}")


# =========================================================
# BOT STARTUP
# =========================================================

@bot.event
async def on_ready():
    global _setup_done

    print(f"🤖 Bot online: {bot.user}")

    if not _setup_done:
        _setup_done = True

        # Đăng ký persistent buttons
        bot.add_view(AccountPanelView())

        try:
            synced = await bot.tree.sync()

            print(
                f"✅ Synced {len(synced)} slash command(s)."
            )

        except Exception as e:
            print(f"❌ Slash command sync error: {e}")

    # Kiểm tra kết nối MongoDB
    try:
        await db.command("ping")
        print("🍃 MongoDB connected.")
    except Exception as e:
        print(f"❌ MongoDB connection error: {e}")


# =========================================================
# MAIN
# =========================================================

async def main():
    await start_web_server()

    try:
        await bot.start(TOKEN)
    finally:
        await mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped.")
