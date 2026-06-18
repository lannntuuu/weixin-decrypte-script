import sqlite3
import hashlib
import os
import re
import shutil
import tempfile
import zstandard
from datetime import datetime
from typing import Optional, List, Tuple

from models import (
    Message, Contact, ContactList, ChatRoom, ChatRoomList,
    ChatRoomUser, Session, SessionList, talker_to_table_name,
)


def decompress_zstd(data: bytes) -> bytes:
    if not data or len(data) < 4:
        return data
    if data[:4] == b'\x28\xb5\x2f\xfd':
        dctx = zstandard.ZstdDecompressor()
        return dctx.decompress(data, max_output_size=100 * 1024 * 1024)
    return data


def safe_decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        decompressed = decompress_zstd(data)
        try:
            return decompressed.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return decompressed.decode("gbk")
            except UnicodeDecodeError:
                return decompressed.hex()
    return str(data)


def read_varint(buf, pos):
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def parse_protobuf(data: bytes) -> dict:
    fields = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = read_varint(data, pos)
        except Exception:
            break
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, pos = read_varint(data, pos)
            fields[field_number] = value
        elif wire_type == 1:
            value = data[pos:pos + 8]
            pos += 8
            fields[field_number] = value
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            value = data[pos:pos + length]
            pos += length
            fields[field_number] = value
        elif wire_type == 5:
            value = data[pos:pos + 4]
            pos += 4
            fields[field_number] = value
        else:
            break
    return fields


def parse_room_data(ext_buffer: bytes) -> List[ChatRoomUser]:
    users = []
    try:
        fields = parse_protobuf(ext_buffer)
        for field_num, value in fields.items():
            if isinstance(value, bytes):
                inner = parse_protobuf(value)
                username = ""
                display_name = ""
                avatar = ""
                for k, v in inner.items():
                    if isinstance(v, bytes):
                        try:
                            v = v.decode("utf-8")
                        except UnicodeDecodeError:
                            v = v.hex()
                    if k == 1:
                        username = str(v)
                    elif k == 2:
                        display_name = str(v)
                    elif k == 3:
                        avatar = str(v)
                if username:
                    users.append(ChatRoomUser(username=username, display_name=display_name, avatar=avatar))
    except Exception:
        pass
    return users


class DBService:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._temp_dir = tempfile.mkdtemp(prefix="weixin_api_")
        self._db_cache = {}
        self._contact_db = None
        self._session_db = None
        self._media_db = None
        self._message_dbs = []
        self._name2id_cache = {}       # rowid -> user_name
        self._talker_table_map = {}    # user_name -> [(db_path, table_name)]，直接从数据库扫描建立
        self._contact_cache = {}
        self._chatroom_cache = {}
        self._initialized = False

    def init(self):
        self._discover_dbs()
        self._load_name2id()
        self._load_contacts()
        self._load_chatrooms()
        self._initialized = True

    def _discover_dbs(self):
        self._message_dbs = []
        for root, dirs, files in os.walk(self.data_dir):
            for f in files:
                path = os.path.join(root, f)
                if f == "contact.decrypted.db" or f == "contact.db":
                    if f.endswith(".decrypted.db"):
                        self._contact_db = path
                elif f == "session.decrypted.db" or f == "session.db":
                    if f.endswith(".decrypted.db"):
                        self._session_db = path
                elif f.startswith("message_") and f.endswith(".decrypted.db"):
                    self._message_dbs.append(path)
                elif f == "hardlink.decrypted.db":
                    self._media_db = path

        self._message_dbs.sort()
        print(f"[DBService] contact_db: {self._contact_db}")
        print(f"[DBService] session_db: {self._session_db}")
        print(f"[DBService] message_dbs: {len(self._message_dbs)} files")
        print(f"[DBService] media_db: {self._media_db}")

    def _open_db(self, path: str) -> sqlite3.Connection:
        temp_path = os.path.join(self._temp_dir, os.path.basename(path))
        if not os.path.exists(temp_path):
            shutil.copy2(path, temp_path)

        conn = sqlite3.connect(temp_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_name2id(self):
        for db_path in self._message_dbs:
            try:
                conn = self._open_db(db_path)
                cursor = conn.cursor()

                # 加载 Name2Id 映射
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Name2Id'")
                rowid2name = {}
                if cursor.fetchone():
                    cursor.execute("SELECT rowid, user_name FROM Name2Id")
                    for row in cursor.fetchall():
                        self._name2id_cache[row[0]] = row[1]
                        rowid2name[row[0]] = row[1]

                # 扫描所有 Msg_ 表，通过 rowid 反查 user_name，建立映射
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
                msg_tables = [row[0] for row in cursor.fetchall()]
                for table_name in msg_tables:
                    # Msg_<MD5> 中的 MD5 可能是对 rowid 做的，尝试反查
                    md5_hex = table_name[4:]  # 去掉 "Msg_" 前缀
                    matched_user = None
                    for rowid, user_name in rowid2name.items():
                        if hashlib.md5(str(rowid).encode()).hexdigest() == md5_hex:
                            matched_user = user_name
                            break
                    # 也尝试对 user_name 做 MD5
                    if matched_user is None:
                        for rowid, user_name in rowid2name.items():
                            if hashlib.md5(user_name.encode()).hexdigest() == md5_hex:
                                matched_user = user_name
                                break

                    if matched_user:
                        if matched_user not in self._talker_table_map:
                            self._talker_table_map[matched_user] = []
                        self._talker_table_map[matched_user].append((db_path, table_name))

            except Exception as e:
                print(f"[DBService] Error loading Name2Id from {db_path}: {e}")

        print(f"[DBService] talker_table_map: {len(self._talker_table_map)} talkers mapped")

    def _load_contacts(self):
        if not self._contact_db:
            return
        try:
            conn = self._open_db(self._contact_db)
            cursor = conn.cursor()
            cursor.execute("SELECT username, alias, remark, nick_name, local_type FROM contact")
            for row in cursor.fetchall():
                c = Contact(
                    username=row[0] or "",
                    alias=row[1] or "",
                    remark=row[2] or "",
                    nick_name=row[3] or "",
                    is_friend=row[4] != 3 if row[4] else True,
                )
                self._contact_cache[c.username] = c
        except Exception as e:
            print(f"[DBService] Error loading contacts: {e}")

    def _load_chatrooms(self):
        if not self._contact_db:
            return
        try:
            conn = self._open_db(self._contact_db)
            cursor = conn.cursor()
            cursor.execute("SELECT username, owner, ext_buffer FROM chat_room")
            for row in cursor.fetchall():
                users = []
                user2display = {}
                if row[2] and isinstance(row[2], bytes):
                    users = parse_room_data(row[2])
                    user2display = {u.username: u.display_name for u in users if u.display_name}

                cr = ChatRoom(
                    name=row[0] or "",
                    owner=row[1] or "",
                    users=users,
                    user2display_name=user2display,
                )
                if row[0] in self._contact_cache:
                    cr.remark = self._contact_cache[row[0]].remark
                    cr.nick_name = self._contact_cache[row[0]].nick_name
                self._chatroom_cache[row[0]] = cr
        except Exception as e:
            print(f"[DBService] Error loading chatrooms: {e}")

        # 从消息数据库的 Name2Id 中补充 chat_room 表缺失的群聊
        for user_name in self._name2id_cache.values():
            if user_name.endswith("@chatroom") and user_name not in self._chatroom_cache:
                cr = ChatRoom(name=user_name)
                if user_name in self._contact_cache:
                    cr.remark = self._contact_cache[user_name].remark
                    cr.nick_name = self._contact_cache[user_name].nick_name
                self._chatroom_cache[user_name] = cr

        print(f"[DBService] chatrooms: {len(self._chatroom_cache)} loaded")

    def get_contact_display(self, username: str) -> str:
        if username in self._contact_cache:
            return self._contact_cache[username].display_name()
        return username

    def get_messages(self, talker: str, start_time: Optional[str] = None,
                     end_time: Optional[str] = None, sender: str = "",
                     keyword: str = "", limit: int = 50, offset: int = 0) -> Tuple[List[Message], int]:
        if not talker:
            return [], 0

        talkers = [t.strip() for t in talker.split(",") if t.strip()]
        senders = [s.strip() for s in sender.split(",") if s.strip()] if sender else []

        start_ts = self._parse_time(start_time) if start_time else 0
        end_ts = self._parse_time(end_time, end=True) if end_time else 9999999999

        all_messages = []
        for talker_id in talkers:
            # 优先使用预建的映射表直接定位
            table_entries = self._talker_table_map.get(talker_id, [])
            if table_entries:
                for db_path, table_name in table_entries:
                    try:
                        msgs = self._query_table(db_path, table_name, talker_id,
                                                 start_ts, end_ts, senders, keyword)
                        all_messages.extend(msgs)
                    except Exception as e:
                        print(f"[DBService] Error querying {db_path}/{table_name}: {e}")
            else:
                # 回退：遍历所有 db 文件尝试查找
                for db_path in self._message_dbs:
                    try:
                        msgs = self._query_messages_from_db(db_path, [talker_id],
                                                            start_ts, end_ts, senders, keyword)
                        all_messages.extend(msgs)
                    except Exception as e:
                        print(f"[DBService] Error querying {db_path}: {e}")

        all_messages.sort(key=lambda m: m.seq)

        total = len(all_messages)
        if limit > 0:
            end_idx = min(offset + limit, total)
            all_messages = all_messages[offset:end_idx]

        return all_messages, total

    def _query_table(self, db_path: str, table_name: str, talker: str,
                     start_ts: int, end_ts: int,
                     senders: List[str], keyword: str) -> List[Message]:
        """已知 db_path 和 table_name 时直接查询，跳过表名查找"""
        conn = self._open_db(db_path)
        cursor = conn.cursor()

        is_chatroom = talker.endswith("@chatroom")

        query = f"""
            SELECT m.sort_seq, m.server_id, m.local_type, m.real_sender_id,
                   m.create_time, m.message_content, m.packed_info_data, m.status
            FROM {table_name} m
            WHERE m.create_time >= ? AND m.create_time <= ?
            ORDER BY m.sort_seq ASC
        """
        try:
            cursor.execute(query, (start_ts, end_ts))
        except sqlite3.OperationalError:
            query = f"""
                SELECT m.local_id, m.server_id, m.local_type, m.real_sender_id,
                       m.create_time, m.message_content, m.packed_info_data, m.status
                FROM {table_name} m
                WHERE m.create_time >= ? AND m.create_time <= ?
                ORDER BY m.local_id ASC
            """
            cursor.execute(query, (start_ts, end_ts))

        messages = []
        for row in cursor.fetchall():
            msg = self._build_message(row, talker, is_chatroom, keyword, senders)
            if msg:
                messages.append(msg)

        return messages

    def _build_message(self, row, talker: str, is_chatroom: bool,
                       keyword: str, senders: List[str]) -> Optional[Message]:
        """从数据库行构建 Message 对象，返回 None 表示应跳过"""
        sort_seq = row[0]
        local_type = row[2]
        real_sender_id = row[3]
        create_time = row[4]
        message_content = row[5]
        packed_info_data = row[6]
        status = row[7]

        sender_name = self._name2id_cache.get(real_sender_id, "")
        if not sender_name:
            sender_name = str(real_sender_id)

        is_self = status == 2 or (not is_chatroom and talker != sender_name)

        raw_content = safe_decode(message_content)

        msg = Message(
            seq=sort_seq,
            time=datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "",
            talker=talker,
            is_chatroom=is_chatroom,
            sender=sender_name,
            is_self=is_self,
            type=local_type,
        )

        if is_chatroom and raw_content:
            split = raw_content.split(":\n", 1)
            if len(split) == 2:
                msg.sender = split[0]
                raw_content = split[1]

        if not msg.is_self:
            if msg.talker in self._chatroom_cache:
                cr = self._chatroom_cache[msg.talker]
                msg.talker_name = cr.display_name()
                if msg.sender in cr.user2display_name:
                    msg.sender_name = cr.user2display_name[msg.sender]
            if not msg.sender_name and msg.sender in self._contact_cache:
                msg.sender_name = self._contact_cache[msg.sender].display_name()
        else:
            msg.sender_name = "me"

        msg.parse_media_info(raw_content)

        if packed_info_data and isinstance(packed_info_data, bytes):
            try:
                pb = parse_protobuf(packed_info_data)
                for fn, fv in pb.items():
                    if isinstance(fv, bytes):
                        inner = parse_protobuf(fv)
                        for ik, iv in inner.items():
                            if isinstance(iv, bytes):
                                try:
                                    iv = iv.decode("utf-8")
                                except UnicodeDecodeError:
                                    iv = iv.hex()
                            if ik == 2 and msg.type == 3:
                                msg.contents["path"] = f"msg/attach/{hashlib.md5(talker.encode()).hexdigest()}/{msg.time[:7]}/Img/{iv}"
                            elif ik == 2 and msg.type == 43:
                                msg.contents["path"] = f"msg/video/{msg.time[:7]}/{iv}"
            except Exception:
                pass

        if senders and msg.sender not in senders and msg.sender_name not in senders:
            return None

        if keyword:
            if keyword.lower() not in msg.content.lower() and keyword.lower() not in raw_content.lower():
                return None

        return msg

    def _query_messages_from_db(self, db_path: str, talkers: List[str],
                                 start_ts: int, end_ts: int,
                                 senders: List[str], keyword: str) -> List[Message]:
        """回退方法：遍历 db 文件尝试通过 MD5 猜测表名"""
        conn = self._open_db(db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        messages = []
        for talker in talkers:
            table_name = talker_to_table_name(talker)
            if table_name not in tables:
                continue

            is_chatroom = talker.endswith("@chatroom")

            query = f"""
                SELECT m.sort_seq, m.server_id, m.local_type, m.real_sender_id,
                       m.create_time, m.message_content, m.packed_info_data, m.status
                FROM {table_name} m
                WHERE m.create_time >= ? AND m.create_time <= ?
                ORDER BY m.sort_seq ASC
            """
            try:
                cursor.execute(query, (start_ts, end_ts))
            except sqlite3.OperationalError:
                query = f"""
                    SELECT m.local_id, m.server_id, m.local_type, m.real_sender_id,
                           m.create_time, m.message_content, m.packed_info_data, m.status
                    FROM {table_name} m
                    WHERE m.create_time >= ? AND m.create_time <= ?
                    ORDER BY m.local_id ASC
                """
                cursor.execute(query, (start_ts, end_ts))

            for row in cursor.fetchall():
                msg = self._build_message(row, talker, is_chatroom, keyword, senders)
                if msg:
                    messages.append(msg)

        return messages

    def get_contacts(self, keyword: str = "", limit: int = 50, offset: int = 0) -> ContactList:
        contacts = list(self._contact_cache.values())

        if keyword:
            kw = keyword.lower()
            contacts = [c for c in contacts if
                        kw in c.username.lower() or
                        kw in (c.alias or "").lower() or
                        kw in (c.remark or "").lower() or
                        kw in (c.nick_name or "").lower()]

        total = len(contacts)
        if limit > 0:
            contacts = contacts[offset:offset + limit]

        return ContactList(items=contacts, total=total)

    def get_chatrooms(self, keyword: str = "", limit: int = 50, offset: int = 0) -> ChatRoomList:
        chatrooms = list(self._chatroom_cache.values())

        if keyword:
            kw = keyword.lower()
            chatrooms = [cr for cr in chatrooms if
                         kw in cr.name.lower() or
                         kw in (cr.remark or "").lower() or
                         kw in (cr.nick_name or "").lower()]

        total = len(chatrooms)
        if limit > 0:
            chatrooms = chatrooms[offset:offset + limit]

        return ChatRoomList(items=chatrooms, total=total)

    def get_sessions(self, keyword: str = "", limit: int = 50, offset: int = 0) -> SessionList:
        if not self._session_db:
            return SessionList()

        try:
            conn = self._open_db(self._session_db)
            cursor = conn.cursor()

            query = "SELECT username, summary, last_timestamp, last_msg_sender, last_sender_display_name FROM SessionTable"
            args = []

            if keyword:
                query += " WHERE username = ? OR last_sender_display_name LIKE ?"
                args = [keyword, f"%{keyword}%"]

            query += " ORDER BY sort_timestamp DESC"

            if limit > 0:
                query += f" LIMIT {limit}"
                if offset > 0:
                    query += f" OFFSET {offset}"

            cursor.execute(query, args)
            sessions = []
            for row in cursor.fetchall():
                username = row[0] or ""
                summary = row[1] or ""
                last_ts = row[2] or 0
                time_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S") if last_ts else ""

                nick_name = row[4] or ""
                if not nick_name and username in self._contact_cache:
                    nick_name = self._contact_cache[username].display_name()

                sessions.append(Session(
                    username=username,
                    nick_name=nick_name,
                    content=summary,
                    time=time_str,
                ))

            return SessionList(items=sessions, total=len(sessions))
        except Exception as e:
            print(f"[DBService] Error loading sessions: {e}")
            return SessionList()

    def _parse_time(self, time_str: str, end: bool = False) -> int:
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                if end and fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=23, minute=59, second=59)
                return int(dt.timestamp())
            except ValueError:
                continue

        try:
            return int(time_str)
        except ValueError:
            return 0 if not end else 9999999999

    def search_messages(self, keyword: str, limit: int = 50, offset: int = 0) -> Tuple[List[dict], int]:
        if not keyword:
            return [], 0

        all_messages = []
        for db_path in self._message_dbs:
            try:
                conn = self._open_db(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]
                msg_tables = [t for t in tables if t.startswith("Msg_")]

                for table in msg_tables:
                    try:
                        cursor.execute(f"""
                            SELECT sort_seq, server_id, local_type, real_sender_id,
                                   create_time, message_content, status
                            FROM {table}
                            WHERE message_content LIKE ?
                            ORDER BY create_time DESC
                            LIMIT ?
                        """, (f"%{keyword}%", limit))
                        for row in cursor.fetchall():
                            raw_content = safe_decode(row[5])
                            sender_name = self._name2id_cache.get(row[3], str(row[3]))
                            msg = {
                                "seq": row[0],
                                "server_id": row[1],
                                "type": row[2],
                                "sender_id": row[3],
                                "sender_name": sender_name,
                                "time": datetime.fromtimestamp(row[4]).strftime("%Y-%m-%d %H:%M:%S") if row[4] else "",
                                "content": raw_content[:500],
                            }
                            all_messages.append(msg)
                    except Exception:
                        pass
            except Exception:
                pass

        all_messages.sort(key=lambda m: m.get("time", ""), reverse=True)
        total = len(all_messages)
        all_messages = all_messages[offset:offset + limit]

        return all_messages, total

    def close(self):
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception:
            pass
