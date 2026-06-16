# 曼波 TTS 插件 (Manbo TTS)

![GitHub release](https://img.shields.io/github/v/release/Qiscard/astrbot_plugin_manbo-tts)

基于 [synapse.fan](https://www.synapse.fan/zh/wormhole/ai/tts) TTS API 的文本转语音插件，支持多种音色切换与音频缓存。

> 原 milorapart API 已失效，现切换到 synapse.fan API

## 指令

| 指令 | 说明 |
|------|------|
| `/tts <文本>` | 使用当前会话音色进行语音合成 |
| `/tts <音色>` | 切换会话音色 |
| `/tts <音色> <文本>` | 临时指定音色进行语音合成（不切换会话） |
| `/tts-list` | 查看缓存音频列表 |
| `/mbxz <id>` | 直接下载指定 ID 的音频 |

### 音色列表

| 音色 | 别名 | 切换指令 |
|------|------|----------|
| 曼波 | `mb` | `/tts mb` |
| 莲莲 | `lian` | `/tts lian` |
| 天皇 | `th` | `/tts th` |
| 老爹 | `ld` | `/tts ld` |
| 播报员 | `bby` | `/tts bby` |
| 科比 | `kb` | `/tts kb` |

## 配置

在 AstrBot 插件管理面板中配置：

| 配置项 | 类型 | 说明 | 默认值 |
|--------|------|------|--------|
| `cookie` | 字符串 | synapse.fan 登录 Cookie（必填） | "" |
| `voice` | 下拉选择 | 默认音色 | manbo |
| `cache_enabled` | 布尔 | 是否启用音频缓存 | true |
| `custom_api_url` | 字符串 | 自定义 TTS API（可选） | "" |

### Cookie 获取方法

1. 在浏览器中登录 [synapse.fan](https://www.synapse.fan/zh/wormhole/ai/tts)
2. 按 F12 打开开发者工具 → Network（网络）选项卡
3. 在 TTS 页面输入文本点击生成，找到 `/api/ai/tts` 请求
4. 从请求头中复制完整的 `Cookie` 值
5. 粘贴到插件配置的 `cookie` 字段中

> Cookie 过期后需要重新获取并更新配置。

## 缓存相关

缓存目录：
```
data/plugin_data/astrbot_plugin_manbo_tts/audio_cache/
```

缓存文件以 `{音色}:{文本}` 的 MD5 命名，不同音色的同一段文字会独立缓存。

## 版本

v2.0.0 — 完全重写对接 synapse.fan API，支持多音色切换