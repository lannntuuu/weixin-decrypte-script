# CLAUDE.md — 微信解密脚本

## 项目概述

Windows 微信 4.x 本地数据解密工具包。从微信进程内存中提取加密密钥，解密 SQLCipher 4 加密数据库和 DAT 图片文件，读取/解压聊天消息，并通过 Flask REST API 暴露所有数据。

## 架构

```
微信进程内存 → scan_keys / find_image_key → 密钥
密钥 + .db 文件 → decrypt_db / decrypt_engine → .decrypted.db
密钥 + .dat 文件 → decrypt_dat → 解密后的图片
.decrypted.db → db_service + models → 结构化数据
db_service → api_server (Flask REST API，端口 5050)
```

## 核心模块

| 文件 | 职责 |
|---|---|
| `scan_keys.py` | 独立脚本：从微信内存中提取 SQLCipher 数据库密钥 |
| `decrypt_db.py` | 独立脚本：解密 SQLCipher 4 数据库 |
| `decrypt_engine.py` | 库模块：密钥扫描 + 数据库解密（供 api_server 导入） |
| `decrypt_dat.py` | 解密 DAT 图片（自动识别 XOR/V1/V2 格式） |
| `find_image_key.py` | 一次性 V2 图片 AES 密钥扫描器 |
| `monitor_image_key.py` | 持续监控 V2 密钥（3 秒间隔） |
| `read_messages.py` | 读取解密数据库，解压 ZSTD，解析 protobuf |
| `models.py` | 数据模型：Message、Contact、ChatRoom、Session |
| `db_service.py` | 数据库服务层，带内存缓存 |
| `api_server.py` | Flask REST API，支持一键自动解密启动 |

## 技术细节

- **数据库加密**：SQLCipher 4，AES-256-CBC，HMAC-SHA512，PBKDF2（2 次迭代用于 MAC 密钥派生）。页大小 4096，每页保留 80 字节（16 IV + 64 HMAC）。
- **图片加密**：XOR（单字节异或）/ V1（固定 AES-128-ECB 密钥 `cfcd208495d565ef`）/ V2（运行时从内存获取 AES-128-ECB 密钥）。通过 6 字节文件头自动识别格式。
- **消息压缩**：ZSTD（魔数 `28 B5 2F FD`）。
- **消息表命名**：`Msg_<MD5(talker_id)>`，通过 `Name2Id` 映射表关联。
- **群聊消息**：内容以 `sender_id:\n` 为前缀。
- **数据库访问**：DBService 打开数据库前先复制到临时目录，避免与微信锁冲突。

## 依赖

```bash
pip install pymem psutil pycryptodome zstandard flask flask-cors
```

## 运行方式

**一键启动 API 服务：**
```bash
python api_server.py <数据目录> [--key <十六进制密钥>] [--key-file <密钥文件路径>] [--no-decrypt] [--host 0.0.0.0] [--port 5050]
```

**分步独立工作流：**
```bash
python scan_keys.py                           # 提取数据库密钥
python decrypt_db.py --auto <目录> found_keys.txt  # 解密数据库
python read_messages.py <解密后的.db>          # 读取消息
python find_image_key.py <附件目录>           # 查找 V2 AES 密钥
python decrypt_dat.py --batch <目录> <输出目录> --aes-key <密钥> --xor-key <0xNN>  # 解密图片
```

## API 接口

- `GET /health` — 健康检查
- `GET /api/v1/chatlog?talker=<id>` — 聊天记录
- `GET /api/v1/contact` — 联系人
- `GET /api/v1/chatroom` — 群聊
- `GET /api/v1/session` — 会话列表
- `GET /api/v1/search?keyword=<关键词>` — 搜索消息

所有接口支持 `format=json|csv|text` 参数。

## 代码约定

- 仅限 Windows：使用 `pymem`、`psutil`、`ctypes`/`kernel32` 访问进程内存。
- `decrypt_engine.py` 复用了 `scan_keys.py` + `decrypt_db.py` 的逻辑但封装为可导入模块——修改解密逻辑时需同步更新。
- Python 3.10+（models.py 中使用了 `match` 语句）。
- 当前无测试套件。

## 注意事项

1. 所有回答以中文输出
2. 需要给出改造思路再动手
3. 样式优先保持现有视觉风格
4. 关键部分要有注解
