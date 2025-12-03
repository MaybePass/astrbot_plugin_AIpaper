import os
import json
import re
import time
import asyncio
import feedparser
import fitz  # PyMuPDF
import aiohttp
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter, MessageChain
from astrbot.api.message_components import Plain, Image, Node, Nodes, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter

class Main(star.Star):
    def __init__(self, context: star.Context, config: dict = None):
        super().__init__(context, config)
        if not config:
            config = {}
        self.config = config
        
        # 配置读取
        self.target_groups = self._parse_target_groups(self.config.get("target_groups", ""))
        self.push_time = self.config.get("push_time", "09:00")
        self.proxy = self.config.get("proxy", "")
        if not self.proxy:
            self.proxy = None
        
        # 读取额外消息配置
        self.extra_message = self.config.get("extra_message", "")

        # 默认提示词模板
        default_prompt = (
            "你是一个专业的 AI 论文解读助手。\n\n"
            "论文标题: {title}\n"
            "作者: {authors}\n"
            "摘要: {abstract}\n\n"
            "论文内容片段:\n{full_text}\n\n"
            "请严格按照以下 Markdown 格式输出，不要输出其他寒暄语：\n\n"
            "## 💡 核心创新点\n(简要概括)\n\n"
            "## 📖 论文概要\n(通俗解释这篇论文解决了什么问题，用了什么方法)\n\n"
            "## 👥 作者背景\n(根据作者姓名简要介绍其所属机构或知名代表作，如果无法确定则略过)\n\n"
            "## 🔬 关键结论\n(实验结果或理论贡献)"
        )
        self.prompt_template = self.config.get("prompt_template", default_prompt)
        
        # 初始化路径
        self.plugin_dir = os.path.dirname(__file__)
        self.history_file = os.path.join(self.plugin_dir, "history.json")
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        self.history = self._load_history()
        
        # 定时任务
        self.scheduler = AsyncIOScheduler()
        try:
            hour, minute = map(int, self.push_time.split(":"))
            self.scheduler.add_job(self.run_daily_push, 'cron', hour=hour, minute=minute)
            self.scheduler.start()
            logger.info(f"AI论文推送定时任务已启动: {self.push_time}, 目标群: {self.target_groups}")
        except Exception as e:
            logger.error(f"定时任务启动失败，请检查时间格式(HH:MM): {e}")

    def _parse_target_groups(self, config_str):
        if not config_str:
            return []
        return [g.strip() for g in re.split(r'[,，]', str(config_str)) if g.strip()]

    def _load_history(self):
        if os.path.exists(self.history_file):
            with open(self.history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []

    def _save_history(self):
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.history, f)

    async def _call_arxiv_api(self, query_url):
        logger.debug(f"Requesting ArXiv: {query_url}, Proxy: {self.proxy}")
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.get(query_url, proxy=self.proxy, timeout=30) as response:
                    if response.status != 200:
                        logger.error(f"ArXiv API error: {response.status}")
                        return None
                    data = await response.text()
                    return feedparser.parse(data)
            except Exception as e:
                logger.error(f"Network error fetching arxiv: {e}")
                return None

    async def fetch_latest_paper(self):
        url = "http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CV+OR+cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=50"
        feed = await self._call_arxiv_api(url)
        
        if not feed or not feed.entries:
            logger.warning("ArXiv API 返回为空或解析失败")
            return None

        for entry in feed.entries:
            paper_id = entry.id.split('/')[-1]
            if paper_id not in self.history:
                return self._parse_entry(entry, paper_id)
        
        logger.warning("最近 50 篇论文都已推送过。")
        return None

    async def fetch_specific_paper(self, query: str):
        id_pattern = r"(\d{4}\.\d{4,5})"
        match = re.search(id_pattern, query)
        
        url = ""
        if match:
            paper_id = match.group(1)
            url = f"http://export.arxiv.org/api/query?id_list={paper_id}"
        else:
            safe_query = query.replace(" ", "+")
            url = f"http://export.arxiv.org/api/query?search_query=ti:{safe_query}&max_results=1"

        feed = await self._call_arxiv_api(url)
        if not feed or not feed.entries:
            return None
        
        entry = feed.entries[0]
        paper_id = entry.id.split('/')[-1]
        return self._parse_entry(entry, paper_id)

    def _parse_entry(self, entry, paper_id):
        return {
            "id": paper_id,
            "title": entry.title.replace('\n', ' '),
            "summary": entry.summary,
            "authors": [a.name for a in entry.authors],
            "link": entry.link,
            "pdf_link": entry.link.replace("abs", "pdf")
        }

    async def process_pdf(self, pdf_url, paper_id):
        pdf_path = os.path.join(self.temp_dir, f"{paper_id}.pdf")
        img_path = os.path.join(self.temp_dir, f"{paper_id}.png")
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                logger.info(f"Downloading PDF: {pdf_url}")
                async with session.get(pdf_url, proxy=self.proxy, timeout=90) as resp:
                    if resp.status == 200:
                        with open(pdf_path, 'wb') as f:
                            f.write(await resp.read())
                    else:
                        logger.error(f"PDF download status: {resp.status}")
                        return None, None
            except Exception as e:
                logger.error(f"PDF Download failed: {e}")
                return None, None
        
        try:
            doc = fitz.open(pdf_path)
            text_content = ""
            for page in doc[:2]: 
                text_content += page.get_text()
            
            page = doc.load_page(0)
            pix = page.get_pixmap(dpi=150)
            pix.save(img_path)
            doc.close()
            os.remove(pdf_path)
            return text_content, img_path
        except Exception as e:
            logger.error(f"PDF processing failed: {e}")
            return None, None

    async def translate_title(self, title):
        provider = self.context.get_using_provider()
        if not provider:
            return title
        
        try:
            prompt = f"Please translate the following scientific paper title into Chinese. Only output the translated title, do not output anything else.\n\nTitle: {title}"
            response = await provider.text_chat(prompt=prompt)
            cn_title = response.completion_text.strip().strip('"').strip("'")
            return cn_title
        except Exception as e:
            logger.warning(f"Title translation failed: {e}")
            return title

    async def get_ai_summary(self, title, abstract, full_text, authors):
        provider = self.context.get_using_provider()
        if not provider:
            return "错误：未配置 AI 模型。"
            
        full_text_snippet = full_text[:3000]
        authors_str = ", ".join(authors)
        
        prompt = self.prompt_template.format(
            title=title,
            abstract=abstract,
            full_text=full_text_snippet,
            authors=authors_str
        )
        
        try:
            response = await provider.text_chat(prompt=prompt)
            return response.completion_text
        except Exception as e:
            return f"AI 解读生成失败: {e}"

    async def _broadcast_message(self, message_chain: MessageChain):
        if not self.target_groups:
            return
        platforms = self.context.platform_manager.get_insts()
        adapter = next((p for p in platforms if isinstance(p, AiocqhttpAdapter)), None)
        
        if not adapter:
            logger.error("未找到 aiocqhttp (QQ) 适配器，无法发送群消息。")
            return

        for group_id in self.target_groups:
            try:
                logger.info(f"正在发送到群: {group_id}")
                await AiocqhttpMessageEvent.send_message(
                    bot=adapter.bot,
                    message_chain=message_chain,
                    is_group=True,
                    session_id=group_id
                )
                await asyncio.sleep(2) 
            except Exception as e:
                logger.error(f"发送到群 {group_id} 失败: {e}")

    async def _execute_push(self, paper, target_umo=None, is_manual=False, silent_start=False):
        """
        执行推送逻辑。
        :param silent_start: 是否静默开始（不发送“正在获取...”）
        """
        
        # 1. 发送提示
        start_msg = MessageChain([Plain(f"📄 正在获取论文: {paper['title']} ...")])
        
        if is_manual:
            if target_umo:
                await self.context.send_message(target_umo, start_msg)
        elif not silent_start:
            if self.target_groups:
                await self._broadcast_message(start_msg)
        else:
            logger.info(f"正在后台处理论文: {paper['title']} ...")

        # 2. 处理内容
        pdf_task = self.process_pdf(paper['pdf_link'], paper['id'])
        trans_task = self.translate_title(paper['title'])
        
        results = await asyncio.gather(pdf_task, trans_task)
        (raw_text, img_path), cn_title = results
        
        if not raw_text or not img_path:
            err_msg = MessageChain([Plain("⚠️ PDF 下载或解析失败。")])
            if is_manual and target_umo:
                await self.context.send_message(target_umo, err_msg)
            elif not is_manual:
                logger.error("PDF 处理失败")
            return

        # 3. 生成总结
        explanation = await self.get_ai_summary(paper['title'], paper['summary'], raw_text, paper['authors'])
        
        # 4. 获取 Bot ID
        try:
            self_uin = self.context.platform_manager.get_insts()[0].client_self_id
        except IndexError:
            self_uin = "10000" 

        display_title = f"{cn_title}\n{paper['title']}"

        # Node 1: 论文预览
        node1_content: list[BaseMessageComponent] = [
            Plain(f"📄 标题:\n{display_title}\n\n"),
            Plain(f"👥 作者: {', '.join(paper['authors'][:3])} et al.\n"),
            Plain(f"🔗 链接: {paper['link']}\n"),
            Image.fromFileSystem(img_path)
        ]
        node1 = Node(name="论文预览", uin=self_uin, content=node1_content)
        
        # Node 2: AI 解读
        node2_content: list[BaseMessageComponent] = [
            Plain(f"解读一下~\n\n{explanation}")
        ]
        node2 = Node(name="AI 助手", uin=self_uin, content=node2_content)
        
        # 5. 组装 Nodes 列表
        all_nodes = [node1, node2]

        # Node 3: 额外消息 (如果配置了)
        if self.extra_message and self.extra_message.strip():
            node3_content: list[BaseMessageComponent] = [
                Plain(self.extra_message)
            ]
            node3 = Node(name="补充信息", uin=self_uin, content=node3_content)
            all_nodes.append(node3)
        
        # 6. 发送消息
        try:
            nodes_component = Nodes(all_nodes)
            forward_msg = MessageChain([nodes_component])
            end_msg = MessageChain([Plain("今日 AI 论文已送达~")]) # 后置消息
            
            if is_manual and target_umo:
                await self.context.send_message(target_umo, forward_msg)
                await asyncio.sleep(1)
                await self.context.send_message(target_umo, end_msg) 
            elif not is_manual:
                await self._broadcast_message(forward_msg)
                await asyncio.sleep(1)
                await self._broadcast_message(end_msg)
            
            if not is_manual:
                self.history.append(paper['id'])
                self._save_history()
                
            if img_path and os.path.exists(img_path):
                os.remove(img_path)
            logger.info("论文推送成功")
                
        except Exception as e:
            logger.error(f"消息发送失败: {e}")
            # 降级
            fallback_msg = MessageChain([
                Plain(f"📄 {display_title}\n{paper['link']}\n\n"),
                Image.fromFileSystem(img_path),
                Plain(f"\n\n{explanation}")
            ])
            if self.extra_message:
                fallback_msg.chain.append(Plain(f"\n\n{self.extra_message}"))

            if is_manual and target_umo:
                await self.context.send_message(target_umo, fallback_msg)
            elif not is_manual:
                await self._broadcast_message(fallback_msg)

    async def run_daily_push(self):
        """每日定时任务"""
        logger.info(">>> 开始执行每日论文推送任务")
        
        if not self.target_groups:
            logger.warning("未配置推送目标群 (target_groups)，任务跳过。")
            return
            
        try:
            paper = await self.fetch_latest_paper()
            if not paper:
                logger.warning("未获取到新论文")
                return
            
            await self._execute_push(paper, is_manual=False, silent_start=True)
            
        except Exception as e:
            logger.error(f"定时推送任务异常: {e}")

    # --- 指令部分 ---

    @filter.command("paper_push")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def push_specific(self, event: AstrMessageEvent, query: str):
        """推送指定论文"""
        if not query:
            yield event.plain_result("请输入论文链接、标题或 ID。")
            return

        yield event.plain_result("🔍 正在 ArXiv 检索论文信息...")
        paper = await self.fetch_specific_paper(query)
        
        if not paper:
            yield event.plain_result("❌ 未在 ArXiv 上找到相关论文，请检查输入。")
            return
            
        target = event.unified_msg_origin
        await self._execute_push(paper, target_umo=target, is_manual=True)

    @filter.command("paper_push_now")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def push_now(self, event: AstrMessageEvent):
        """立即触发自动推送"""
        paper = await self.fetch_latest_paper()
        if not paper:
            yield event.plain_result("没有获取到新的待推送论文。")
            return
            
        await self._execute_push(paper, target_umo=event.unified_msg_origin, is_manual=True, silent_start=False)