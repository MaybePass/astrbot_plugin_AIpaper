import os
import json
import re
import asyncio
import aiohttp
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 导入 fitz 和 feedparser，但不在主线程直接调用耗时操作
import feedparser
import fitz  # PyMuPDF

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter, MessageChain
from astrbot.api.message_components import Plain, Image, Node, Nodes, BaseMessageComponent
from astrbot.api.star import StarTools
from astrbot.core.platform.astr_message_event import MessageSession
from astrbot.core.platform.message_type import MessageType

class Main(star.Star):
    def __init__(self, context: star.Context, config: dict = None):
        super().__init__(context, config)
        if not config:
            config = {}
        self.config = config
        
        # 1. 配置读取与健壮性检查
        self.target_groups = self._parse_target_groups(self.config.get("target_groups"))
        self.push_time = self.config.get("push_time", "09:00")
        self.proxy = self.config.get("proxy", "") or None
        self.extra_message = self.config.get("extra_message", "")
        
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

        # 2. 数据持久化规范 (StarTools.get_data_dir)
        # 使用插件名作为目录名，AstrBot 会自动在 data/plugin_data/ 下创建
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_daily_paper")
        self.history_file = os.path.join(self.data_dir, "history.json")
        self.temp_dir = os.path.join(self.data_dir, "temp")
        
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        self.history = self._load_history()
        
        # 3. 定时任务初始化
        self.scheduler = AsyncIOScheduler()
        try:
            hour, minute = map(int, self.push_time.split(":"))
            self.scheduler.add_job(self.run_daily_push, 'cron', hour=hour, minute=minute)
            self.scheduler.start()
            logger.info(f"AI论文推送定时任务已启动: {self.push_time}, 目标群: {self.target_groups}")
        except Exception as e:
            logger.error(f"定时任务启动失败，请检查时间格式(HH:MM): {e}")

    def _parse_target_groups(self, config_val):
        """解析目标群组，支持字符串或列表"""
        if not config_val:
            return []
        if isinstance(config_val, list):
            return [str(g).strip() for g in config_val]
        if isinstance(config_val, str):
            return [g.strip() for g in re.split(r'[,，]', config_val) if g.strip()]
        return []

    def _load_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_history(self):
        # 放在同步方法里调用，但在实际写入时考虑放入线程池（虽然写小json很快，但为了最佳实践）
        # 这里为了简化，我们假设数据量不大。如果严格遵循规范：
        # asyncio.create_task(asyncio.to_thread(self._write_history_sync))
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f)
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")

    # --- 异步/线程池包装器 ---

    async def _parse_feed_in_thread(self, data):
        return await asyncio.to_thread(feedparser.parse, data)

    async def _process_pdf_in_thread(self, pdf_path, img_path):
        """在线程池中处理 PDF，避免阻塞主循环"""
        def _heavy_work():
            doc = None
            try:
                doc = fitz.open(pdf_path)
                # 提取前2页文本
                text_content = ""
                for page in doc[:2]: 
                    text_content += page.get_text()
                
                # 第一页转图片
                page = doc.load_page(0)
                pix = page.get_pixmap(dpi=150)
                pix.save(img_path)
                return text_content
            except Exception as e:
                logger.error(f"Fitz processing error: {e}")
                return None
            finally:
                if doc:
                    doc.close()
        
        return await asyncio.to_thread(_heavy_work)

    # --- 核心逻辑 ---

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
                    # 使用线程池解析 Feed
                    return await self._parse_feed_in_thread(data)
            except Exception as e:
                logger.error(f"Network error fetching arxiv: {e}")
                return None

    async def fetch_latest_paper(self):
        url = "http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CV+OR+cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=50"
        feed = await self._call_arxiv_api(url)
        
        if not feed or not feed.entries:
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
        """下载并处理 PDF"""
        pdf_path = os.path.join(self.temp_dir, f"{paper_id}.pdf")
        img_path = os.path.join(self.temp_dir, f"{paper_id}.png")
        
        # 增强 PDF 下载逻辑：增加 Headers，处理超时
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            try:
                logger.info(f"Downloading PDF from: {pdf_url}")
                # 增加超时时间到 90s，部分 PDF 下载较慢
                async with session.get(pdf_url, proxy=self.proxy, timeout=90) as resp:
                    if resp.status == 200:
                        # 使用异步文件写入
                        content = await resp.read()
                        # 写入文件是 I/O 操作，虽然小文件可以接受，但大文件最好也放线程池
                        # 这里为了简化直接写，因为是内存 -> 磁盘
                        with open(pdf_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"PDF Downloaded: {len(content)} bytes")
                    else:
                        logger.error(f"PDF download status: {resp.status}")
                        return None, None
            except Exception as e:
                logger.error(f"PDF Download failed: {e}")
                return None, None
        
        # 在线程池中处理 PDF 解析
        text_content = await self._process_pdf_in_thread(pdf_path, img_path)
        
        # 清理 PDF 文件
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
            
        if not text_content:
            logger.error("PDF parsing returned empty text")
            return None, None
            
        return text_content, img_path

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

    async def _broadcast_to_groups(self, message_chain: MessageChain):
        """使用通用接口广播到配置的群组"""
        if not self.target_groups:
            return
            
        # 获取所有平台实例
        platforms = self.context.platform_manager.get_insts()
        
        # 优先寻找 aiocqhttp 适配器 (QQ)，因为目前主要针对 QQ 群
        # 如果未来要支持其他平台，这里需要修改逻辑来匹配群号所属的平台
        target_platform = None
        for p in platforms:
            if p.meta().name == "aiocqhttp" or p.meta().name == "napcat":
                 target_platform = p
                 break
        
        if not target_platform:
            logger.error("未找到支持 QQ 群消息的平台适配器 (aiocqhttp/napcat)。")
            return

        platform_id = target_platform.meta().id # 平台实例ID

        for group_id in self.target_groups:
            try:
                logger.info(f"正在发送到群: {group_id}")
                # 构造 UMO 字符串: platform_id:GroupMessage:group_id
                # 注意：aiocqhttp 的 session_id 通常就是 group_id
                
                # 使用 context.send_message 发送
                # 这需要构造一个 MessageSession 对象或者符合格式的字符串
                session_str = f"{platform_id}:GroupMessage:{group_id}"
                
                await self.context.send_message(session_str, message_chain)
                
                # 避免风控
                await asyncio.sleep(2) 
            except Exception as e:
                logger.error(f"发送到群 {group_id} 失败: {e}")

    async def _execute_push(self, paper, target_umo=None, is_manual=False, silent_start=False):
        """执行推送逻辑"""
        
        # 1. 发送提示
        if not silent_start and (is_manual or self.target_groups):
            start_msg = MessageChain([Plain(f"📄 正在获取论文: {paper['title']} ...")])
            if is_manual and target_umo:
                await self.context.send_message(target_umo, start_msg)
            elif not is_manual:
                await self._broadcast_to_groups(start_msg)
        
        # 2. 处理内容 (try-finally 确保清理)
        img_path = None
        try:
            pdf_task = self.process_pdf(paper['pdf_link'], paper['id'])
            trans_task = self.translate_title(paper['title'])
            
            results = await asyncio.gather(pdf_task, trans_task)
            (raw_text, img_path), cn_title = results
            
            if not raw_text or not img_path:
                err_msg = MessageChain([Plain("⚠️ PDF 下载或解析失败，请检查网络配置或代理设置。")])
                if is_manual and target_umo:
                    await self.context.send_message(target_umo, err_msg)
                elif not is_manual:
                    await self._broadcast_to_groups(err_msg)
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
                Plain(f"🤖 AI 深度解读\n\n{explanation}")
            ]
            node2 = Node(name="AI 助手", uin=self_uin, content=node2_content)
            
            all_nodes = [node1, node2]

            # Node 3: 额外消息
            if self.extra_message and self.extra_message.strip():
                node3_content: list[BaseMessageComponent] = [Plain(self.extra_message)]
                node3 = Node(name="补充信息", uin=self_uin, content=node3_content)
                all_nodes.append(node3)
            
            # 5. 发送消息
            try:
                nodes_component = Nodes(all_nodes)
                forward_msg = MessageChain([nodes_component])
                end_msg = MessageChain([Plain("📅 今日 AI 论文已送达~")])
                
                if is_manual and target_umo:
                    await self.context.send_message(target_umo, forward_msg)
                    if self.extra_message: # 只有在手动模式下，为了不打扰，我们只在有额外消息时才发第二条，或者根据需求调整
                         pass # 手动模式就不发 "今日已送达" 了
                elif not is_manual:
                    # 定时任务广播
                    await self._broadcast_to_groups(forward_msg)
                    await asyncio.sleep(2)
                    await self._broadcast_to_groups(end_msg)
                
                # 记录历史
                if not is_manual:
                    self.history.append(paper['id'])
                    self._save_history()
                
                logger.info(f"论文 {paper['title']} 推送成功")
                    
            except Exception as e:
                logger.error(f"消息发送失败 (尝试降级发送): {e}")
                # 降级: 拆开发送
                fallback_chain = MessageChain([
                    Plain(f"📄 {display_title}\n{paper['link']}\n\n"),
                    Image.fromFileSystem(img_path),
                    Plain(f"\n\n{explanation}")
                ])
                if self.extra_message:
                    fallback_chain.chain.append(Plain(f"\n\n{self.extra_message}"))

                if is_manual and target_umo:
                    await self.context.send_message(target_umo, fallback_chain)
                elif not is_manual:
                    await self._broadcast_to_groups(fallback_chain)
                    
        finally:
            # 5. 清理临时文件
            if img_path and os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception as e:
                    logger.warning(f"清理图片失败: {e}")

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
            
            # silent_start=True: 不发送“正在获取...”
            await self._execute_push(paper, is_manual=False, silent_start=True)
            
        except Exception as e:
            logger.error(f"定时推送任务异常: {e}")

    # --- 指令部分 ---

    @filter.command("paper_set_group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group(self, event: AstrMessageEvent):
        """添加当前群到推送列表"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用此指令。")
            return

        if group_id in self.target_groups:
             yield event.plain_result("当前群聊已在推送列表中。")
             return

        self.target_groups.append(group_id)
        
        # 更新配置
        plugin_md = self.context.get_registered_star("astrbot_plugin_daily_paper")
        if plugin_md and plugin_md.config:
             # 保存为字符串格式，逗号分隔
             plugin_md.config["target_groups"] = ",".join(self.target_groups)
             plugin_md.config.save_config()
        
        yield event.plain_result(f"✅ 已添加群 {group_id} 到每日推送列表。")

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
        """立即触发自动推送 (手动测试)"""
        # 移除“正在触发...”回复
        
        paper = await self.fetch_latest_paper()
        if not paper:
            yield event.plain_result("没有获取到新的待推送论文。")
            return
            
        # is_manual=True, silent_start=False (显示“正在获取”)
        await self._execute_push(paper, target_umo=event.unified_msg_origin, is_manual=True, silent_start=False)