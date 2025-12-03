# AstrBot Daily AI Paper | 每日 AI 论文推送插件

这是一个为 [AstrBot](https://github.com/Soulter/AstrBot) 设计的插件，能够每日定时自动从 ArXiv 获取最新的 AI 领域（cs.AI, cs.CV, cs.CL）热门论文，生成中文解读，并推送到指定的 QQ 群。

## ✨ 功能特性

*   **自动推送**：每日定时自动抓取 ArXiv 最新论文。
*   **智能去重**：自动记录已推送过的论文 ID，避免重复推送。
*   **深度解读**：调用 AstrBot 配置的 LLM（大语言模型）生成通俗易懂的中文解读，包含核心创新点、论文概要、关键结论等。
*   **图文并茂**：自动下载论文 PDF 并截取第一页作为预览图，配合原文链接和 AI 解读，以**合并转发消息**的形式发送，阅读体验极佳。
*   **多群支持**：支持配置多个推送目标群组。
*   **手动触发**：支持通过指令手动推送指定论文（通过 ArXiv ID、链接或标题）。
*   **高度可配**：支持自定义推送时间、提示词模板、代理地址等。
*   **额外消息**：支持在合并转发消息末尾追加自定义文本（如广告、免责声明）。

## 📦 安装与配置

### 1. 安装依赖

在 AstrBot 的 `data/plugins/astrbot_plugin_daily_paper` 目录下，运行：

```bash
pip install -r requirements.txt
```

*注意：`pymupdf` (fitz) 可能需要系统层面的依赖库，如果在安装或运行时报错，请参考 PyMuPDF 的官方文档。*

### 2. 配置文件

插件首次加载后，会自动在 WebUI 的插件配置页生成配置项。你也可以手动编辑 `data/config/astrbot_plugin_daily_paper_config.json`。

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `target_groups` | string | `""` | 每日推送的目标群号列表，多个群号用英文逗号分隔 (例如: `123456,789012`) |
| `push_time` | string | `"09:00"` | 每日自动推送的时间 (格式 `HH:MM`) |
| `proxy` | string | `""` | 访问 ArXiv 的 HTTP 代理地址 (如 `http://127.0.0.1:7890`)，国内用户建议配置，留空则直连 |
| `extra_message` | text | `""` | 合并转发消息中的第三条额外内容（如广告、免责声明等），留空则不发送 |
| `prompt_template` | text | (见下文) | AI 总结提示词模板。可用变量: `{title}`, `{abstract}`, `{full_text}`, `{authors}` |

**默认提示词模板：**

```text
你是一个专业的 AI 论文解读助手。

论文标题: {title}
作者: {authors}
摘要: {abstract}

论文内容片段:
{full_text}

请严格按照以下 Markdown 格式输出，不要输出其他寒暄语：

## 💡 核心创新点
(简要概括)

## 📖 论文概要
(通俗解释这篇论文解决了什么问题，用了什么方法)

## 👥 作者背景
(根据作者姓名简要介绍其所属机构或知名代表作，如果无法确定则略过)

## 🔬 关键结论
(实验结果或理论贡献)
```

## 💻 指令使用

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `/paper_push <内容>` | 管理员 | 手动推送指定论文到当前会话。<br>支持输入：<br>1. ArXiv ID (如 `2310.06825`)<br>2. ArXiv 链接 (如 `https://arxiv.org/abs/2310.06825`)<br>3. 论文标题 (如 `Attention Is All You Need`) |
| `/paper_push_now` | 管理员 | 立即触发一次“每日推荐”流程。会自动从 ArXiv 获取一篇未推送过的新论文发到当前会话，用于测试效果。 |
| `/paper_set_group` | 管理员 | (快捷指令) 将当前所在的群聊添加为每日推送目标。建议在 WebUI 中直接修改 `target_groups` 配置以管理多个群。 |

## 🛠️ 常见问题

**Q: 为什么无法发送合并转发消息？**
A: 请确保你使用的是支持 OneBot V11 协议的适配器（如 NapCat, Lagrange.OneBot 等），且 AstrBot 已正确连接。Telegram 平台暂不支持此格式的合并转发，插件会自动降级为普通多条消息发送。

**Q: ArXiv 连接超时？**
A: ArXiv 在国内访问不稳定。请在插件配置中填写有效的 HTTP 代理地址（`proxy` 字段）。

**Q: 报错 `ModuleNotFoundError: No module named 'frontend'`?**
A: 这是 PyMuPDF 的依赖问题，请尝试 `pip install pymupdf` 或 `pip install fitz`。确保没有安装名为 `fitz` 的旧包（应卸载 `fitz` 并安装 `pymupdf`）。

## 📄 License

MIT
```
