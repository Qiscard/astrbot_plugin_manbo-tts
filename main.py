import aiohttp
import asyncio
import hashlib
import json
import os
import pathlib
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs
from typing import Optional, Dict
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.core.message.components as Comp

# Manbo TTS API 信息 (已切换到 synapse.fan)
SYNAPSE_TTS_API_URL = "https://www.synapse.fan/api/ai/tts"  # TTS API 端点
SYNAPSE_CDN_BASE = "https://cdn.synapse.fan/TTS"  # 音频 CDN 基础 URL
MAX_TEXT_LENGTH = 200  # 设置最大文本长度，避免请求过长
ALLOWED_DOMAINS = ["cdn.synapse.fan"]  # 允许的音频 URL 域名白名单
TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=20)  # 全局超时设置（基础等待已含在总超时内）
CDN_RETRY_MAX = 10  # 等待音频生成时的最大重试次数
CDN_RETRY_DELAY = 0.8  # 每次重试间隔（秒）

# 音色短名映射
VOICE_ALIAS = {
    "mb": "manbo",
    "lian": "lianlian",
    "th": "tianhuang",
    "ld": "laodie",
    "bby": "bobaoyuan",
    "kb": "kobe",
}


class ManboTTSPlugin(Star):
    PLUGIN_NAME = "astrbot_plugin_manbo_tts"

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}
        self.cookie = self.config.get("cookie", "")
        self.voice = self.config.get("voice", "manbo")
        self.cache_enabled = self.config.get("cache_enabled", True)
        self.custom_api_url = self.config.get("custom_api_url", "")
        # 当前会话音色（可通过 /tts mb 等指令切换，初始为配置默认值）
        self.current_voice = self.voice
        # 提取自定义API的域名用于URL验证
        self.custom_api_domain = ""
        if self.custom_api_url:
            try:
                parsed = urlparse(self.custom_api_url)
                self.custom_api_domain = parsed.netloc
            except Exception as e:
                logger.warning(f"解析自定义API URL失败: {e}")

        # 根据AstrBot规范，大文件存储在 data/plugin_data/{plugin_name}/ 目录下
        # 不再提供自定义缓存目录选项，所有缓存文件统一存储到规范目录
        data_path = pathlib.Path(get_astrbot_data_path())
        self.cache_dir = str((data_path / "plugin_data" / self.PLUGIN_NAME / "audio_cache").resolve())
        self.mapping_file = str(pathlib.Path(self.cache_dir) / "md5_mapping.json")

        logger.info(f"Cookie配置: {'已设置' if self.cookie else '未设置'}")
        logger.info(f"默认音色: {self.voice}")
        logger.info(f"缓存功能启用: {self.cache_enabled}")
        logger.info(f"缓存目录（规范路径）: {self.cache_dir}")
        logger.info(f"映射文件路径: {self.mapping_file}")

        self.session = None
        self.lock = asyncio.Lock()  # 用于session管理的锁
        self.mapping_lock = asyncio.Lock()  # 用于映射文件管理的锁

    @filter.on_astrbot_loaded()  # 插件加载完成后初始化 session
    async def on_loaded(self):
        """插件初始化，创建一个全局的 session"""
        logger.info(f"插件加载完成，开始初始化")
        logger.info(f"缓存功能状态: {self.cache_enabled}")

        async with self.lock:
            if not self.session or self.session.closed:
                logger.info("初始化aiohttp session")
                self.session = aiohttp.ClientSession()
                logger.info("aiohttp session初始化完成")

        # 确保缓存目录存在
        if self.cache_enabled:
            logger.info("缓存功能已启用，准备缓存目录")
            cache_path = pathlib.Path(self.cache_dir)
            logger.info(f"缓存目录路径: {cache_path.absolute()}")
            cache_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"缓存目录已准备：{cache_path.absolute()}")

            # 初始化映射文件并迁移现有缓存
            logger.info("开始初始化映射文件")
            await self._init_mapping_file()
            logger.info("映射文件初始化完成")
        else:
            logger.info("缓存功能未启用，跳过映射文件初始化")

    async def _init_mapping_file(self):
        """初始化映射文件并迁移现有缓存"""
        logger.info(f"开始初始化映射文件: {self.mapping_file}")
        mapping_path = pathlib.Path(self.mapping_file)
        cache_dir_path = pathlib.Path(self.cache_dir)

        logger.info(f"映射文件路径: {mapping_path}, 是否存在: {mapping_path.exists()}")
        logger.info(f"缓存目录路径: {cache_dir_path}, 是否存在: {cache_dir_path.exists()}")

        if not mapping_path.exists():
            # 创建空的映射文件
            logger.info(f"映射文件不存在，创建新的映射文件")
            await self._save_mapping({})
            logger.info(f"创建新的映射文件完成: {self.mapping_file}")
        else:
            # 加载现有映射
            logger.info(f"映射文件已存在，加载现有映射")
            mapping = await self._load_mapping()
            logger.info(f"加载现有映射文件完成，包含 {len(mapping)} 条记录")

        # 迁移现有缓存文件（扫描.wav文件，确保所有文件都在映射中）
        logger.info("开始迁移现有缓存文件")
        await self._migrate_existing_cache()
        logger.info("迁移现有缓存文件完成")

    async def _load_mapping(self) -> Dict[str, str]:
        """加载映射文件"""
        mapping_path = pathlib.Path(self.mapping_file)
        if not mapping_path.exists():
            return {}

        try:
            async with self.mapping_lock:
                with open(self.mapping_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加载映射文件失败: {e}")
            return {}

    async def _save_mapping(self, mapping: Dict[str, str]):
        """保存映射文件"""
        try:
            async with self.mapping_lock:
                with open(self.mapping_file, 'w', encoding='utf-8') as f:
                    json.dump(mapping, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存映射文件失败: {e}")

    async def _add_to_mapping(self, md5_hash: str, text: str):
        """添加新的映射记录"""
        mapping = await self._load_mapping()
        mapping[md5_hash] = text
        await self._save_mapping(mapping)
        logger.debug(f"添加映射记录: {md5_hash} -> {text[:50]}...")

    async def _remove_from_mapping(self, md5_hash: str):
        """从映射中移除记录"""
        mapping = await self._load_mapping()
        if md5_hash in mapping:
            del mapping[md5_hash]
            await self._save_mapping(mapping)
            logger.debug(f"移除映射记录: {md5_hash}")

    async def _migrate_existing_cache(self):
        """迁移现有缓存文件，确保所有.wav文件都在映射中，并清理不存在的映射条目"""
        cache_dir_path = pathlib.Path(self.cache_dir)
        mapping = await self._load_mapping()
        updated = False
        cleaned = False

        # 获取所有.wav文件的MD5哈希
        existing_files = {wav_file.stem for wav_file in cache_dir_path.glob("*.wav")}

        # 1. 添加缺失的映射条目
        for md5_hash in existing_files:
            if md5_hash not in mapping:
                # 添加未知文本标记
                mapping[md5_hash] = "[unknown]"
                updated = True
                logger.info(f"迁移现有缓存文件: {md5_hash}.wav -> [unknown]")

        # 2. 清理不存在的映射条目
        mapping_keys = list(mapping.keys())
        for md5_hash in mapping_keys:
            if md5_hash not in existing_files:
                del mapping[md5_hash]
                cleaned = True
                logger.info(f"清理不存在的映射条目: {md5_hash}")

        if updated or cleaned:
            await self._save_mapping(mapping)
            if updated:
                logger.info(f"新增 {len([k for k, v in mapping.items() if v == '[unknown]' and k in existing_files])} 条未知记录")
            if cleaned:
                logger.info(f"清理了 {len([k for k in mapping_keys if k not in existing_files])} 个不存在的映射条目")

    async def _download_file(self, url: str, dest_path: pathlib.Path, text: str = "") -> bool:
        """下载文件到指定路径，支持重试等待音频生成完成"""
        # 确保 session 已初始化
        if not self.session or self.session.closed:
            async with self.lock:
                if not self.session or self.session.closed:
                    self.session = aiohttp.ClientSession()

        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        headers["Referer"] = "https://www.synapse.fan/"
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        for attempt in range(1, CDN_RETRY_MAX + 1):
            try:
                async with self.session.get(url, headers=headers, timeout=TIMEOUT) as response:
                    if response.status == 200:
                        with open(dest_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                        logger.info(f"文件已下载：{dest_path}")

                        # 添加映射记录（如果传入了文本并且是缓存场景）
                        if text:
                            md5_hash = dest_path.stem
                            await self._add_to_mapping(md5_hash, text)

                        return True
                    elif response.status == 404 and attempt < CDN_RETRY_MAX:
                        logger.info(f"音频尚未生成（第{attempt}次），{CDN_RETRY_DELAY}秒后重试...")
                        await asyncio.sleep(CDN_RETRY_DELAY)
                    else:
                        logger.error(f"下载文件失败，状态码：{response.status}（第{attempt}次）")
                        return False
            except Exception as e:
                logger.error(f"下载文件异常：{str(e)}（第{attempt}次）")
                if attempt < CDN_RETRY_MAX:
                    await asyncio.sleep(CDN_RETRY_DELAY)
                else:
                    if dest_path.exists():
                        dest_path.unlink()
                    return False

        return False

    def _build_custom_api_url(self, text: str) -> str:
        """构建自定义API的完整URL"""
        parsed = urlparse(self.custom_api_url)
        # 获取现有查询参数
        existing_params = parse_qs(parsed.query)
        # 添加或覆盖参数
        existing_params['text'] = [text]
        existing_params['text_language'] = ['zh']
        # 构建新查询字符串
        new_query = urlencode(existing_params, doseq=True)
        # 重建URL
        new_parsed = parsed._replace(query=new_query)
        # 确保路径部分不为空（避免http://example.com?query形式）
        if not new_parsed.path:
            new_parsed = new_parsed._replace(path='/')
        return urlunparse(new_parsed)

    async def fetch_audio_url(self, text_to_convert, voice_override=None):
        """异步获取音频 URL，使用 synapse.fan TTS API
        voice_override: 临时指定的音色，为 None 时使用配置的默认音色
        """
        # 双重检查锁定：首先检查 session 状态，只有在未初始化或已关闭时才加锁
        if not self.session or self.session.closed:
            async with self.lock:
                if not self.session or self.session.closed:
                    logger.info("Session 未初始化或已关闭，正在初始化...")
                    self.session = aiohttp.ClientSession()

        try:
            # 检查 session 是否已关闭，避免抛出 RuntimeError
            if self.session.closed:
                logger.error("会话已关闭，无法继续请求。")
                return None

            # 根据配置选择 API
            if self.custom_api_url:
                logger.info(f"使用自定义 API，文本长度：{len(text_to_convert)}")
                # 构建自定义 API 请求 URL
                audio_url = self._build_custom_api_url(text_to_convert)
                # 验证 URL 是否允许
                if self.is_valid_url(audio_url):
                    return audio_url
                else:
                    logger.error(f"自定义 API URL 未通过验证：{audio_url}")
                    return None
            else:
                # 使用 synapse.fan API
                voice = voice_override if voice_override else self.voice
                logger.info(f"使用 synapse.fan TTS API，文本长度：{len(text_to_convert)}, 音色：{voice}")

                # 构造请求体
                payload = {
                    "text": text_to_convert,
                    "voice": voice
                }

                # 构造请求头
                headers = {
                    "Content-Type": "application/json"
                }
                if not self.cookie:
                    logger.error("Cookie 未配置，无法调用 synapse.fan API。请在插件配置中填入登录 Cookie。")
                    return None
                headers["Cookie"] = self.cookie

                async with self.session.post(
                    SYNAPSE_TTS_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=TIMEOUT,
                ) as response:
                    if response.status != 200:
                        body_text = await response.text()
                        logger.error(f"synapse.fan API 请求失败，状态码：{response.status}，响应：{body_text}")
                        return None

                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError:
                        logger.error("响应内容不是有效的 JSON")
                        return None

                    # 从响应中提取 id 字段
                    audio_id = data.get("id")
                    if not audio_id:
                        logger.error(f"API 响应中缺少 'id' 字段：{data}")
                        return None

                    # 拼接 CDN 音频 URL
                    audio_url = f"{SYNAPSE_CDN_BASE}/{audio_id}.wav"
                    logger.info(f"生成的音频 URL: {audio_url}")

                    # 验证 URL 安全性
                    if self.is_valid_url(audio_url):
                        return audio_url
                    else:
                        logger.error(f"非法的音频 URL：{audio_url}")
                        return None

        except asyncio.TimeoutError:
            logger.error("请求超时")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"请求异常：{str(e)}")
            return None
        except RuntimeError as e:
            logger.error(f"会话已关闭，无法请求音频：{str(e)}")
            return None

    def is_valid_url(self, url):
        """校验 URL 是否为有效的外部 URL，避免 SSRF"""
        try:
            parsed_url = urlparse(url)
            logger.info(f"URL 校验: 原始URL={url}, 协议={parsed_url.scheme}, 域名={parsed_url.netloc}")
            allowed_domains = ALLOWED_DOMAINS.copy()
            if self.custom_api_domain:
                allowed_domains.append(self.custom_api_domain)
            logger.info(f"允许的域名列表: {allowed_domains}")
            # 校验是否为允许的 http/https 协议和域名
            if parsed_url.scheme in ["http", "https"] and parsed_url.netloc in allowed_domains:
                logger.info("URL 校验通过")
                return True
            logger.error(f"不允许的域名或协议：{parsed_url.netloc}")
            return False
        except Exception as e:
            logger.error(f"URL 校验失败：{str(e)}")
            return False

    @filter.command("manbo-list")
    async def manbo_list(self, event: AstrMessageEvent):
        """列出所有缓存的音频文件及其对应的文本"""
        logger.info(f"执行manbo-list命令")

        if not self.cache_enabled:
            logger.warning("缓存功能未启用，无法列出缓存文件")
            yield event.plain_result("缓存功能未启用，无法列出缓存文件。")
            return

        try:
            # 确保映射文件已初始化
            await self._init_mapping_file()

            mapping = await self._load_mapping()
            logger.info(f"加载映射文件，包含 {len(mapping)} 条记录")

            if not mapping:
                yield event.plain_result("缓存目录为空，暂无缓存文件。")
                return

            # 统计信息
            total_files = len(mapping)
            known_files = len([text for text in mapping.values() if text != "[unknown]"])
            unknown_files = total_files - known_files

            # 构建纯文本输出
            text_content = f"""缓存统计:<br>
总缓存文件数: {total_files}
已知文本文件: {known_files}
未知文本文件: {unknown_files}
"""

            if known_files > 0:
                text_content += "缓存内容列表:<br>"
                row_num = 1
                for md5_hash, text in mapping.items():
                    if text != "[unknown]":
                        display_text = text if len(text) <= 50 else text[:47] + "..."
                        text_content += f"{row_num}. {display_text} | {md5_hash}<br>"
                        row_num += 1

            if unknown_files > 0:
                text_content += f"\n未知文本文件（共{unknown_files}个）:<br>"
                unknown_md5_list = [md5 for md5, text in mapping.items() if text == '[unknown]']
                for md5 in unknown_md5_list[:50]:
                    text_content += f"• {md5}<br>"
                if unknown_files > 50:
                    text_content += f"... 等（共{unknown_files}个）<br>"

            # 使用text_to_image方法渲染为图片
            try:
                logger.info("使用text_to_image方法渲染图片")
                url = await self.text_to_image(text_content)
                yield event.image_result(url)
            except Exception as e:
                logger.warning(f"图片生成失败: {str(e)}，使用纯文本输出")
                # 构建纯文本输出
                output = f"""
                            📊 缓存统计:\n
                            • 总缓存文件数: {total_files}\n
                            • 已知文本文件: {known_files}\n
                            • 未知文本文件: {unknown_files}\n
                            """
                if known_files > 0:
                    output += "📋 缓存内容列表:\n"
                    for i, (md5_hash, text) in enumerate(mapping.items(), 1):
                        if text != "[unknown]":
                            display_text = text if len(text) <= 50 else text[:47] + "..."
                            output += f"{i}. {display_text} \n"
                if unknown_files > 0:
                    output += f"⚠️  有 {unknown_files} 个缓存文件缺少文本信息（可能是旧版本创建的缓存）"
                    unknown_md5_list = [md5 for md5, text in mapping.items() if text == '[unknown]']
                    if unknown_md5_list:
                        output += "   这些文件的MD5哈希为:<br>"
                        for md5 in unknown_md5_list[:20]:
                            output += f"   • {md5}<br>"
                        if unknown_files > 20:
                            output += f"   等（共{unknown_files}个）<br>"
                yield event.plain_result(output)

        except Exception as e:
            logger.error(f"列出缓存文件时发生错误: {str(e)}")
            yield event.plain_result("列出缓存文件时发生错误，请查看日志。")

    @filter.command("tts")
    async def tts(self, event: AstrMessageEvent, text: str):
        """文本转语音（TTS）指令。用法：
        /tts 文本             使用当前音色
        /tts mb              切换音色到 manbo（此后的 /tts 使用此音色）
        /tts lian            切换到莲莲，依此类推
        可用音色：mb(曼波) lian(莲莲) th(天皇) ld(老爹) bby(播报员) kb(科比)
        """
        # 处理文本参数：如果text是字符串，直接使用；如果是列表，拼接
        if isinstance(text, str):
            text_str = text.strip()
        else:
            text_str = " ".join(text).strip()
        logger.info(f"原始text类型: {type(text)}, 内容: {text}")
        logger.info(f"处理后的文本: {text_str}")

        # 校验文本是否为空字符串
        if not text_str:
            yield event.plain_result("请输入要转换为语音的文本！")
            return

        # 特殊处理：如果文本是"list"，提示使用manbo-list命令
        if text_str.lower() == "list":
            yield event.plain_result("请使用 '/manbo-list' 命令查看缓存列表，或输入其他文本进行语音转换。")
            return

        # 检查是否为音色切换指令：/tts mb 或 /tts lian（单个别名/全名，不带额外文本）
        first_word = text_str.split(maxsplit=1)[0].lower()
        rest_after_first = text_str[len(first_word):].strip()

        # 解析出音色名（别名映射）
        target_voice = None
        if first_word in VOICE_ALIAS:
            target_voice = VOICE_ALIAS[first_word]
        elif first_word in VOICE_ALIAS.values():
            target_voice = first_word

        if target_voice and not rest_after_first:
            # 只有音色名，没有额外文本 → 切换音色
            self.current_voice = target_voice
            voice_display = self._get_voice_display(target_voice)
            yield event.plain_result(f"✅ 已切换到音色：{voice_display}")
            return

        if target_voice:
            # /tts mb 哈哈哈 → 使用指定音色，不切换会话状态
            voice = target_voice
            voice_text = rest_after_first
        else:
            # /tts 哈哈哈 → 使用当前会话音色
            voice = self.current_voice
            voice_text = text_str

        # 输入文本长度限制
        if len(voice_text) > MAX_TEXT_LENGTH:
            yield event.plain_result(f"文本长度超过限制（{MAX_TEXT_LENGTH} 字符）。请缩短文本再试。")
            return

        try:
            logger.info(f"处理文本: {voice_text}, 音色: {voice}")
            logger.info(f"缓存功能状态: {self.cache_enabled}")

            # 缓存检查（缓存按原始文本+音色组合的 MD5）
            cache_key_text = f"{voice}:{voice_text}"
            cache_key_hash = hashlib.md5(cache_key_text.encode('utf-8')).hexdigest() + ".wav"
            cache_path = pathlib.Path(self.cache_dir) / cache_key_hash

            if self.cache_enabled and cache_path.exists():
                logger.info(f"使用缓存音频：{cache_path}")
                chain = [Comp.Record(file=str(cache_path))]
                yield event.chain_result(chain)
                return

            if self.cache_enabled:
                logger.info("未找到缓存，将请求API")
            else:
                logger.info("缓存功能已禁用，直接请求API")

            # 计算等待时间：基础1秒 + 每20字加1秒。先等待再请求API让音频有足够生成时间
            text_len = len(voice_text)
            wait_time = 2.0 + (text_len // 20)
            logger.info(f"文本长度 {text_len} 字，等待 {wait_time:.1f} 秒后请求API（让音频有充足生成时间）...")
            await asyncio.sleep(wait_time)

            # 获取音频 URL（传入音色参数）
            audio_url = await self.fetch_audio_url(voice_text, voice)
            if not audio_url:
                yield event.plain_result("无法获取音频文件，接口返回无效数据。")
                return

            # 如果启用缓存，下载到本地
            if self.cache_enabled:
                download_success = await self._download_file(audio_url, cache_path)
                if download_success:
                    chain = [Comp.Record(file=str(cache_path))]
                else:
                    logger.warning("缓存下载失败，尝试直接发送 CDN 音频 URL")
                    chain = [Comp.Record(file=audio_url, url=audio_url)]
            else:
                chain = [Comp.Record(file=audio_url, url=audio_url)]

            yield event.chain_result(chain)

        except Exception as e:
            logger.error(f"处理请求时发生错误: {str(e)}")
            yield event.plain_result("发生了错误，请稍后再试。")

    def _get_voice_display(self, voice: str) -> str:
        """获取音色的显示名"""
        display_map = {
            "manbo": "曼波 (manbo)",
            "lianlian": "莲莲 (lianlian)",
            "tianhuang": "天皇 (tianhuang)",
            "laodie": "老爹 (laodie)",
            "bobaoyuan": "播报员 (bobaoyuan)",
            "kobe": "科比 (kobe)",
        }
        return display_map.get(voice, voice)

    @filter.command("mbxz")
    async def mbxz(self, event: AstrMessageEvent, audio_id: str):
        """通过音频ID直接下载并发送语音"""
        audio_id = audio_id.strip()
        logger.info(f"执行 mbxz 命令，音频ID: {audio_id}")

        if not audio_id:
            yield event.plain_result("请输入音频ID，例如：/mbxz 38074")
            return

        audio_url = f"{SYNAPSE_CDN_BASE}/{audio_id}.wav"
        logger.info(f"从 CDN 下载音频: {audio_url}")

        try:
            # 检查 session
            if not self.session or self.session.closed:
                async with self.lock:
                    if not self.session or self.session.closed:
                        self.session = aiohttp.ClientSession()

            # 下载到本地临时文件（使用音频ID作为文件名）
            cache_dir = pathlib.Path(self.cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            local_path = cache_dir / f"{audio_id}.wav"

            success = await self._download_file(audio_url, local_path)
            if success:
                chain = [Comp.Record(file=str(local_path))]
                yield event.chain_result(chain)
            else:
                # 下载失败，尝试直接发 URL
                logger.warning("下载失败，尝试直接发送 CDN URL")
                chain = [Comp.Record(file=audio_url, url=audio_url)]
                yield event.chain_result(chain)

        except Exception as e:
            logger.error(f"mbxz 命令执行失败: {str(e)}")
            yield event.plain_result("下载音频失败，请检查ID是否正确。")

    async def terminate(self):
        """插件销毁时的清理工作"""
        async with self.lock:  # 添加锁来确保资源清理的并发安全
            if self.session:
                await self.session.close()  # 关闭 session
                self.session = None  # 清空 session