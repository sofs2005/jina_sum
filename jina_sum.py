# encoding:utf-8
import json
import os
import html
import re
from urllib.parse import urlparse, quote
import time

import requests
from newspaper import Article
import newspaper
from bs4 import BeautifulSoup

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *

@plugins.register(
    name="JinaSum",
    desire_priority=20,
    hidden=False,
    desc="Sum url link content with newspaper3k and llm",
    version="2.2",
    author="sofs2005",
)
class JinaSum(Plugin):
    """网页内容总结插件
    
    功能：
    1. 自动总结分享的网页内容
    2. 支持手动触发总结
    3. 支持群聊和单聊不同处理方式
    4. 支持黑名单群组配置
    """
    # 默认配置
    DEFAULT_CONFIG = {
        "max_words": 8000,
        "prompt": "我需要对下面引号内文档进行总结，总结输出包括以下三个部分：\n📖 一句话总结\n🔑 关键要点,用数字序号列出3-5个文章的核心内容\n🏷 标签: #xx #xx\n请使用emoji让你的表达更生动\n\n",
        "white_url_list": [],
        "black_url_list": [
            "https://support.weixin.qq.com",  # 视频号视频
            "https://channels-aladin.wxqcloud.qq.com",  # 视频号音乐
        ],
        "black_group_list": [],
        "auto_sum": True,
        "cache_timeout": 300,  # 缓存超时时间（5分钟）
    }

    def __init__(self):
        """初始化插件配置"""
        try:
            super().__init__()
            
            # 确保使用默认配置初始化
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()
            
            # 使用默认配置初始化
            for key, default_value in self.DEFAULT_CONFIG.items():
                if key not in self.config:
                    self.config[key] = default_value
            
            # 设置配置参数
            self.max_words = self.config.get("max_words", 8000)
            self.prompt = self.config.get("prompt", "我需要对下面引号内文档进行总结...")
            self.cache_timeout = self.config.get("cache_timeout", 300)  # 默认5分钟
            
            # URL黑白名单配置
            self.white_url_list = self.config.get("white_url_list", [])
            self.black_url_list = self.config.get("black_url_list", [])
            self.black_group_list = self.config.get("black_group_list", [])
            
            # 是否自动总结（仅群聊有效）
            self.auto_sum = self.config.get("auto_sum", False)
            
            # 消息缓存
            self.pending_messages = {}  # 用于存储待处理的消息，格式: {chat_id: {"content": content, "timestamp": time.time()}}
            
            # API 设置
            self.open_ai_api_base = "https://api.openai.com/v1"
            self.open_ai_model = "gpt-3.5-turbo"
            
            logger.info(f"[JinaSum] 初始化完成, config={self.config}")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{str(e)}", exc_info=True)
            raise Exception("[JinaSum] 初始化失败")

    def on_handle_context(self, e_context: EventContext):
        """处理消息"""
        context = e_context['context']
        logger.info(f"[JinaSum] 收到消息, 类型={context.type}, 内容长度={len(context.content)}")

        # 首先在日志中记录完整的消息内容，便于调试
        orig_content = context.content
        if len(orig_content) > 500:
            logger.info(f"[JinaSum] 消息内容(截断): {orig_content[:500]}...")
        else:
            logger.info(f"[JinaSum] 消息内容: {orig_content}")
        
        if context.type not in [ContextType.TEXT, ContextType.SHARING]:
            logger.info(f"[JinaSum] 消息类型不符合处理条件，跳过: {context.type}")
            return

        content = context.content
        channel = e_context['channel']
        msg = e_context['context']['msg']
        chat_id = msg.from_user_id
        is_group = msg.is_group
        
        # 打印前50个字符用于调试
        preview = content[:50] + "..." if len(content) > 50 else content
        logger.info(f"[JinaSum] 处理消息: {preview}, 类型={context.type}")

        # 检查内容是否为XML格式（哔哩哔哩等第三方分享卡片）
        if content.startswith('<?xml') or (content.startswith('<msg>') and '<appmsg' in content) or ('<appmsg' in content and '<url>' in content):
            logger.info("[JinaSum] 检测到XML格式分享卡片，尝试提取URL")
            try:
                import xml.etree.ElementTree as ET
                # 处理可能的XML声明
                if content.startswith('<?xml'):
                    content = content[content.find('<msg>'):]
                
                # 如果不是完整的XML，尝试添加根节点
                if not content.startswith('<msg') and '<appmsg' in content:
                    content = f"<msg>{content}</msg>"
                
                # 对于一些可能格式不标准的XML，使用更宽松的解析方式
                try:
                    root = ET.fromstring(content)
                except ET.ParseError:
                    # 尝试用正则表达式提取URL
                    import re
                    url_match = re.search(r'<url>(.*?)</url>', content)
                    if url_match:
                        extracted_url = url_match.group(1)
                        logger.info(f"[JinaSum] 通过正则表达式从XML中提取到URL: {extracted_url}")
                        content = extracted_url
                        context.type = ContextType.SHARING
                        context.content = extracted_url
                    else:
                        logger.error("[JinaSum] 无法通过正则表达式从XML中提取URL")
                        return
                else:
                    # XML解析成功
                    url_elem = root.find('.//url')
                    title_elem = root.find('.//title')
                    
                    # 检查是否有appinfo节点，判断是否为B站等特殊应用
                    appinfo = root.find('.//appinfo')
                    app_name = None
                    if appinfo is not None and appinfo.find('appname') is not None:
                        app_name = appinfo.find('appname').text
                        logger.info(f"[JinaSum] 检测到APP分享: {app_name}")
                    
                    logger.info(f"[JinaSum] XML解析结果: url_elem={url_elem is not None}, title_elem={title_elem is not None}, app_name={app_name}")
                    
                    if url_elem is not None and url_elem.text:
                        # 提取到URL，将类型修改为SHARING
                        extracted_url = url_elem.text
                        logger.info(f"[JinaSum] 从XML中提取到URL: {extracted_url}")
                        content = extracted_url
                        context.type = ContextType.SHARING
                        context.content = extracted_url
                        
                        # 对于B站视频链接，记录额外信息
                        if app_name and ("哔哩哔哩" in app_name or "bilibili" in app_name.lower() or "b站" in app_name):
                            logger.info("[JinaSum] 检测到B站视频分享")
                            # 可以在这里添加B站视频的特殊处理逻辑
                    else:
                        logger.error("[JinaSum] 无法从XML中提取URL")
                        return
            except Exception as e:
                logger.error(f"[JinaSum] 解析XML失败: {str(e)}", exc_info=True)
                return

        # 检查是否需要自动总结
        should_auto_sum = self.auto_sum
        if should_auto_sum and is_group and msg.from_user_nickname in self.black_group_list:
            should_auto_sum = False

        # 清理过期缓存
        self._clean_expired_cache()

        # 处理分享消息
        if context.type == ContextType.SHARING:
            logger.debug("[JinaSum] Processing SHARING message")
            if is_group:
                if should_auto_sum:
                    return self._process_summary(content, e_context, retry_count=0)
                else:
                    self.pending_messages[chat_id] = {
                        "content": content,
                        "timestamp": time.time()
                    }
                    logger.debug(f"[JinaSum] Cached SHARING message: {content}, chat_id={chat_id}")
                    return
            else:  # 单聊消息直接处理
                return self._process_summary(content, e_context, retry_count=0)

        # 处理文本消息
        elif context.type == ContextType.TEXT:
            logger.debug("[JinaSum] Processing TEXT message")
            content = content.strip()
            
            # 移除可能的@信息
            if content.startswith("@"):
                parts = content.split(" ", 1)
                if len(parts) > 1:
                    content = parts[1].strip()
                else:
                    content = ""
            
            # 检查是否包含"总结"关键词（仅群聊需要）
            if is_group and "总结" in content:
                logger.debug(f"[JinaSum] Found summary trigger, pending_messages={self.pending_messages}")
                if chat_id in self.pending_messages:
                    cached_content = self.pending_messages[chat_id]["content"]
                    logger.debug(f"[JinaSum] Processing cached content: {cached_content}")
                    del self.pending_messages[chat_id]
                    return self._process_summary(cached_content, e_context, retry_count=0, skip_notice=False)
                
                # 检查是否是直接URL总结，移除"总结"并检查剩余内容是否为URL
                url = content.replace("总结", "").strip()
                if url and self._check_url(url):
                    logger.debug(f"[JinaSum] Processing direct URL: {url}")
                    return self._process_summary(url, e_context, retry_count=0, skip_notice=False)
                logger.debug("[JinaSum] No content to summarize")
                return

            # 处理"问xxx"格式的追问
            if content.startswith("问"):
                question = content[1:].strip()
                if question:
                    logger.debug(f"[JinaSum] Processing question: {question}")
                    return self._process_question(question, chat_id, e_context)
                else:
                    logger.debug("[JinaSum] Empty question, ignored")
                    return
                    
            # 单聊中直接处理URL
            if not is_group and self._check_url(content):
                return self._process_summary(content, e_context, retry_count=0)

    def _clean_expired_cache(self):
        """清理过期的缓存"""
        current_time = time.time()
        # 清理待处理消息缓存
        expired_keys = [
            k for k, v in self.pending_messages.items() 
            if current_time - v["timestamp"] > self.cache_timeout
        ]
        for k in expired_keys:
            del self.pending_messages[k]

    def _get_content_via_api(self, url):
        """通过API服务获取微信公众号内容
        
        当jina直接访问失败时，使用此备用方法
        
        Args:
            url: 微信文章URL
            
        Returns:
            str: 文章内容
        """
        try:
            # 简单的API调用，参考sum4all插件实现
            api_url = "https://ai.sum4all.site"
            headers = {
                'Content-Type': 'application/json'
            }
            payload = {
                "link": url,
                "prompt": "",  # 不需要总结，只获取内容
            }
            
            logger.debug(f"[JinaSum] Trying to get content via API: {url}")
            response = requests.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            
            response_data = response.json()
            if response_data.get("success"):
                # 从API返回中提取原始内容
                content = response_data.get("content", "")
                if content:
                    logger.debug(f"[JinaSum] Successfully got content via API, length: {len(content)}")
                    return content
            
            logger.error(f"[JinaSum] API returned failure or empty content")
            return None
        except Exception as e:
            logger.error(f"[JinaSum] Error getting content via API: {str(e)}")
            return None

    def _get_content_via_newspaper(self, url):
        """使用newspaper3k库提取文章内容
        
        Args:
            url: 文章URL
            
        Returns:
            str: 文章内容,失败返回None
        """
        try:
            # 处理B站短链接
            if "b23.tv" in url:
                # 先获取重定向后的真实URL
                try:
                    logger.debug(f"[JinaSum] Resolving B站短链接: {url}")
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Cache-Control": "max-age=0",
                        "Connection": "keep-alive"
                    }
                    response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
                    if response.status_code == 200:
                        real_url = response.url
                        logger.debug(f"[JinaSum] B站短链接解析结果: {real_url}")
                        url = real_url
                except Exception as e:
                    logger.error(f"[JinaSum] 解析B站短链接失败: {str(e)}")
            
            # 增强模拟真实浏览器访问
            import random
            
            # 随机选择一个User-Agent，模拟不同浏览器
            user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ]
            selected_ua = random.choice(user_agents)
            
            # 构建更真实的请求头
            headers = {
                "User-Agent": selected_ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0"
            }
            
            # 设置一个随机的引荐来源，微信文章有时需要Referer
            referers = [
                "https://www.baidu.com/",
                "https://www.google.com/",
                "https://www.bing.com/",
                "https://mp.weixin.qq.com/",
                "https://weixin.qq.com/",
                "https://www.qq.com/"
            ]
            if random.random() > 0.3:  # 70%的概率添加Referer
                headers["Referer"] = random.choice(referers)
                
            # 为微信公众号文章添加特殊处理
            if "mp.weixin.qq.com" in url:
                try:
                    # 添加必要的微信Cookie参数，减少被检测的可能性
                    cookies = {
                        "appmsglist_action_3941382959": "card",  # 一些随机的Cookie值
                        "appmsglist_action_3941382968": "card",
                        "pac_uid": f"{int(time.time())}_f{random.randint(10000, 99999)}",
                        "rewardsn": "",
                        "wxtokenkey": f"{random.randint(100000, 999999)}",
                    }
                    
                    # 直接使用requests进行内容获取，有时比newspaper更有效
                    session = requests.Session()
                    response = session.get(url, headers=headers, cookies=cookies, timeout=20)
                    response.raise_for_status()
                    
                    # 使用BeautifulSoup直接解析
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(response.content, 'html.parser')
                    
                    # 微信文章通常有这些特征
                    title_elem = soup.select_one('#activity-name')
                    author_elem = soup.select_one('#js_name') or soup.select_one('#js_profile_qrcode > div > strong')
                    content_elem = soup.select_one('#js_content')
                    
                    if content_elem:
                        # 移除无用元素
                        for remove_elem in content_elem.select('script, style, svg'):
                            remove_elem.extract()
                            
                        # 尝试获取所有文本
                        text_content = content_elem.get_text(separator='\n', strip=True)
                        
                        if text_content and len(text_content) > 200:  # 内容足够长
                            title = title_elem.get_text(strip=True) if title_elem else ""
                            author = author_elem.get_text(strip=True) if author_elem else "未知作者"
                            
                            # 构建完整内容
                            full_content = ""
                            if title:
                                full_content += f"标题: {title}\n"
                            if author and author != "未知作者":
                                full_content += f"作者: {author}\n"
                            full_content += f"\n{text_content}"
                            
                            logger.debug(f"[JinaSum] 成功通过直接请求提取微信文章内容，长度: {len(text_content)}")
                            return full_content
                except Exception as e:
                    logger.error(f"[JinaSum] 直接请求提取微信文章失败: {str(e)}")
                    # 失败后使用newspaper尝试，不要返回
            
            # 配置newspaper
            newspaper.Config().browser_user_agent = selected_ua
            newspaper.Config().request_timeout = 30
            newspaper.Config().fetch_images = False  # 不下载图片以加快速度
            newspaper.Config().memoize_articles = False  # 避免缓存导致的问题
            
            # 对newspaper的下载过程进行定制
            try:
                # 创建Article对象但不立即下载
                article = Article(url, language='zh')
                
                # 手动下载
                session = requests.Session()
                response = session.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                
                # 手动设置html内容
                article.html = response.text
                article.download_state = 2  # 表示下载完成
                
                # 然后解析
                article.parse()
            except Exception as direct_dl_error:
                logger.error(f"[JinaSum] 尝试定制下载失败，回退到标准方法: {str(direct_dl_error)}")
                article = Article(url, language='zh')
                article.download()
                article.parse()
            
            # 尝试获取完整内容
            title = article.title
            authors = ', '.join(article.authors) if article.authors else "未知作者"
            publish_date = article.publish_date.strftime("%Y-%m-%d") if article.publish_date else "未知日期"
            content = article.text
            
            # 如果内容为空或过短，尝试直接从HTML获取
            if not content or len(content) < 500:
                logger.debug("[JinaSum] Article content too short, trying to extract from HTML directly")
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(article.html, 'html.parser')
                    
                    # 移除脚本和样式元素
                    for script in soup(["script", "style"]):
                        script.extract()
                    
                    # 获取所有文本
                    text = soup.get_text(separator='\n', strip=True)
                    
                    # 如果直接提取的内容更长，使用它
                    if len(text) > len(content):
                        content = text
                        logger.debug(f"[JinaSum] Using BeautifulSoup extracted content: {len(content)} chars")
                except Exception as bs_error:
                    logger.error(f"[JinaSum] BeautifulSoup extraction failed: {str(bs_error)}")
            
            # 合成最终内容
            if title:
                full_content = f"标题: {title}\n"
                if authors and authors != "未知作者":
                    full_content += f"作者: {authors}\n"
                if publish_date and publish_date != "未知日期":
                    full_content += f"发布日期: {publish_date}\n"
                full_content += f"\n{content}"
            else:
                full_content = content
            
            if not full_content or len(full_content.strip()) < 50:
                logger.debug("[JinaSum] No content extracted by newspaper")
                return None
            
            # 对于B站视频，尝试获取视频描述
            if "bilibili.com" in url or "b23.tv" in url:
                if title and not content:
                    # 如果只有标题没有内容，至少返回标题
                    return f"标题: {title}\n\n描述: 这是一个B站视频，无法获取完整内容。请直接观看视频。"
            
            logger.debug(f"[JinaSum] Successfully extracted content via newspaper, length: {len(full_content)}")
            return full_content
            
        except Exception as e:
            logger.error(f"[JinaSum] Error extracting content via newspaper: {str(e)}")
            if "mp.weixin.qq.com" in url:
                return f"无法获取微信公众号文章内容。可能原因：\n1. 文章需要登录才能查看\n2. 文章已被删除\n3. 服务器被微信风控\n\n请尝试直接打开链接: {url}"
            return None

    def _process_summary(self, content: str, e_context: EventContext, retry_count: int = 0, skip_notice: bool = False):
        """处理总结请求"""
        try:
            if not self._check_url(content):
                logger.debug(f"[JinaSum] {content} is not a valid url, skip")
                return
                
            if retry_count == 0 and not skip_notice:
                logger.debug("[JinaSum] Processing URL: %s" % content)
                reply = Reply(ReplyType.TEXT, "🎉正在为您生成总结，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            # 获取网页内容
            target_url = html.unescape(content)
            target_url_content = None
            
            # 检查是否包含XML数据（分享消息错误）
            if target_url.startswith("<") and "appmsg" in target_url:
                logger.warning("[JinaSum] 检测到XML数据而不是URL，尝试提取真实URL")
                try:
                    import xml.etree.ElementTree as ET
                    # 处理可能的XML声明
                    if target_url.startswith('<?xml'):
                        target_url = target_url[target_url.find('<msg>'):]
                    
                    root = ET.fromstring(target_url)
                    url_elem = root.find(".//url")
                    if url_elem is not None and url_elem.text:
                        target_url = url_elem.text
                        logger.debug(f"[JinaSum] 从XML中提取到URL: {target_url}")
                    else:
                        logger.error("[JinaSum] 无法从XML中提取URL")
                        raise ValueError("无法从分享卡片中提取URL")
                except Exception as ex:
                    logger.error(f"[JinaSum] 解析XML失败: {str(ex)}")
                    raise ValueError("无法从分享卡片中提取URL")
            
            # 使用newspaper3k提取内容
            logger.debug(f"[JinaSum] 使用newspaper3k提取内容: {target_url}")
            target_url_content = self._get_content_via_newspaper(target_url)
            
            # 检查返回的内容是否包含验证提示
            if target_url_content and target_url_content.startswith("⚠️"):
                # 这是一个验证提示，直接返回给用户
                logger.info(f"[JinaSum] 返回验证提示给用户: {target_url_content}")
                reply = Reply(ReplyType.INFO, target_url_content)
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            # 如果newspaper提取失败，尝试使用API
            if not target_url_content:
                logger.debug(f"[JinaSum] newspaper提取失败，尝试API方法: {target_url}")
                target_url_content = self._get_content_via_api(target_url)
            
            # 如果所有方法都失败
            if not target_url_content:
                # 对于B站视频，提供特殊处理
                if "bilibili.com" in target_url or "b23.tv" in target_url:
                    target_url_content = "这是一个B站视频链接。由于视频内容无法直接提取，请直接点击链接观看视频。"
                else:
                    raise ValueError("无法提取文章内容")
                
            # 清洗内容
            target_url_content = self._clean_content(target_url_content)
            
            # 限制内容长度
            target_url_content = target_url_content[:self.max_words]
            logger.debug(f"[JinaSum] Got content length: {len(target_url_content)}")
            
            # 构造提示词和内容
            sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
            
            try:
                # 直接使用Bridge调用后台获取回复
                from bridge.bridge import Bridge
                
                # 修改context内容
                e_context['context'].type = ContextType.TEXT
                e_context['context'].content = sum_prompt
                
                # 使用Bridge调用后台模型
                logger.debug(f"[JinaSum] 使用Bridge直接调用后台模型，prompt长度={len(sum_prompt)}")
                bridge = Bridge()
                reply_content = bridge.fetch_reply_content(sum_prompt, e_context['context'])
                
                # 检查返回内容
                if reply_content and hasattr(reply_content, 'content'):
                    reply = reply_content  # 如果返回的是Reply对象，直接使用
                else:
                    # 否则创建新的Reply对象
                    if isinstance(reply_content, str):
                        reply = Reply(ReplyType.TEXT, reply_content)
                    else:
                        reply = Reply(ReplyType.ERROR, "后台返回格式错误")
                
                # 设置回复并中断处理链
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                logger.debug(f"[JinaSum] 使用Bridge直接调用后台模型成功，回复类型={reply.type}，长度={len(reply.content) if reply.content else 0}")
                return
                
            except Exception as e:
                logger.warning(f"[JinaSum] 直接调用后台失败: {str(e)}", exc_info=True)
                
                # 如果直接调用失败，回退到插件链的方式
                logger.debug("[JinaSum] 回退到使用插件链处理")
                e_context.action = EventAction.CONTINUE
                return
                
        except Exception as e:
            logger.error(f"[JinaSum] Error in processing summary: {str(e)}")
            if retry_count < 3:
                logger.info(f"[JinaSum] Retrying {retry_count + 1}/3...")
                return self._process_summary(content, e_context, retry_count + 1, True)
            
            # 友好的错误提示
            error_msg = "抱歉，无法获取文章内容。可能是因为:\n"
            error_msg += "1. 文章需要登录或已过期\n"
            error_msg += "2. 文章有特殊的访问限制\n"
            error_msg += "3. 网络连接不稳定\n\n"
            error_msg += "建议您:\n"
            error_msg += "- 直接打开链接查看\n"
            error_msg += "- 稍后重试\n"
            error_msg += "- 尝试其他文章"
            
            reply = Reply(ReplyType.ERROR, error_msg)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def _process_question(self, question: str, chat_id: str, e_context: EventContext, retry_count: int = 0):
        """处理用户提问"""
        try:
            # 获取最近总结的内容
            recent_content = None
            recent_timestamp = 0
            
            # 遍历所有缓存找到最近总结的内容
            for url, cache_data in self.content_cache.items():
                if cache_data["timestamp"] > recent_timestamp:
                    recent_timestamp = cache_data["timestamp"]
                    recent_content = cache_data["content"]
            
            if not recent_content or time.time() - recent_timestamp > self.content_cache_timeout:
                logger.debug(f"[JinaSum] No valid content cache found or content expired")
                return  # 找不到相关文章，让后续插件处理问题
            
            if retry_count == 0:
                reply = Reply(ReplyType.TEXT, "🤔 正在思考您的问题，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            # 准备问答请求
            openai_chat_url = self._get_openai_chat_url()
            openai_headers = self._get_openai_headers()
            
            # 构建问答的 prompt
            qa_prompt = self.qa_prompt.format(
                content=recent_content[:self.max_words],
                question=question
            )
            
            openai_payload = {
                'model': self.open_ai_model,
                'messages': [{"role": "user", "content": qa_prompt}]
            }
            
            # 调用 API 获取回答
            response = requests.post(openai_chat_url, headers=openai_headers, json=openai_payload, timeout=60)
            response.raise_for_status()
            answer = response.json()['choices'][0]['message']['content']
            
            reply = Reply(ReplyType.TEXT, answer)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            
        except Exception as e:
            logger.error(f"[JinaSum] Error in processing question: {str(e)}")
            if retry_count < 3:
                return self._process_question(question, chat_id, e_context, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"抱歉，处理您的问题时出错: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose, **kwargs):
        help_text = "网页内容总结插件:\n"
        help_text += "1. 发送「总结 网址」可以总结指定网页的内容\n"
        help_text += "2. 单聊时分享消息会自动总结\n"
        if self.auto_sum:
            help_text += "3. 群聊中分享消息默认自动总结"
            if self.black_group_list:
                help_text += "（部分群组需要发送含「总结」的消息触发）\n"
            else:
                help_text += "\n"
        else:
            help_text += "3. 群聊中收到分享消息后，发送包含「总结」的消息即可触发总结\n"
        help_text += f"4. 总结完成后5分钟内，可以发送「{self.qa_trigger}xxx」来询问文章相关问题\n"
        help_text += "注：群聊中的分享消息的总结请求需要在60秒内发出"
        return help_text

    def _load_config_template(self):
        """加载配置模板"""
        try:
            template_path = os.path.join(os.path.dirname(__file__), "config.json.template")
            if os.path.exists(template_path):
                with open(template_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"

    def _get_openai_headers(self):
        """获取openai的header"""
        config = super().get_config()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.get('openai_api_key')}"
        }

    def _get_openai_payload(self, target_url_content):
        """构造openai的payload
        
        Args:
            target_url_content: 网页内容
        """
        sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
        messages = [{"role": "user", "content": sum_prompt}]
        payload = {
            'model': self.open_ai_model,
            'messages': messages
        }
        return payload

    def _check_url(self, target_url: str):
        """检查URL是否有效且允许访问
        
        Args:
            target_url: 要检查的URL
            
        Returns:
            bool: URL是否有效且允许访问
        """
        stripped_url = target_url.strip()
        logger.debug(f"[JinaSum] 检查URL: {stripped_url}")
        
        # 简单校验是否是url
        if not stripped_url.startswith("http://") and not stripped_url.startswith("https://"):
            logger.debug("[JinaSum] URL不以http://或https://开头，跳过")
            return False

        # 检测一些常见的不适合总结的内容类型
        skip_patterns = [
            # 视频/音乐平台的非文章内容
            r"(bilibili\.com|b23\.tv).*/video/", # B站视频
            r"(youtube\.com|youtu\.be)/watch", # YouTube视频
            r"(music\.163\.com|y\.qq\.com)/(song|playlist|album)", # 音乐
            
            # 文件链接
            r"\.(pdf|doc|docx|ppt|pptx|xls|xlsx|zip|rar|7z)(\?|$)", # 文档和压缩包
            
            # 图片链接
            r"\.(jpg|jpeg|png|gif|bmp|webp|svg)(\?|$)", # 图片
            
            # 地图
            r"(map\.(baidu|google|qq)\.com)", # 地图
            
            # 工具类
            r"(docs\.qq\.com|shimo\.im|yuque\.com|notion\.so)", # 在线文档
            
            # 社交媒体特定内容
            r"weixin\.qq\.com/[^/]+/([^/]+/){2,}",  # 微信小程序或其他功能
            r"(weibo\.com|t\.cn)/[^/]+/[^/]+",  # 微博
            
            # 商城商品
            r"(taobao\.com|tmall\.com|jd\.com)/.*?(item|product)",  # 电商商品
            
            # 小程序
            r"servicewechat\.com"  # 微信小程序
        ]
        
        # 使用正则表达式检查
        import re
        for pattern in skip_patterns:
            if re.search(pattern, stripped_url, re.IGNORECASE):
                logger.debug(f"[JinaSum] URL匹配跳过模式: {pattern}")
                return False

        # 检查白名单
        if len(self.white_url_list):
            if not any(stripped_url.startswith(white_url) for white_url in self.white_url_list):
                logger.debug("[JinaSum] URL不在白名单中")
                return False

        # 排除黑名单，黑名单优先级>白名单
        for black_url in self.black_url_list:
            if stripped_url.startswith(black_url):
                logger.debug(f"[JinaSum] URL在黑名单中: {black_url}")
                return False

        logger.debug("[JinaSum] URL检查通过")
        return True

    def _clean_content(self, content: str) -> str:
        """清洗内容，去除图片、链接、广告等无用信息
        
        Args:
            content: 原始内容
            
        Returns:
            str: 清洗后的内容
        """
        # 记录原始长度
        original_length = len(content)
        logger.debug(f"[JinaSum] Original content length: {original_length}")
        
        # 移除Markdown图片标签
        content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
        content = re.sub(r'\[!\[.*?\]\(.*?\)', '', content)  # 嵌套图片标签
        
        # 移除图片描述 (通常在方括号或特定格式中)
        content = re.sub(r'\[图片\]|\[image\]|\[img\]|\[picture\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[.*?图片.*?\]', '', content)
        
        # 移除阅读时间、字数等元数据
        content = re.sub(r'本文字数：\d+，阅读时长大约\d+分钟', '', content)
        content = re.sub(r'阅读时长[:：].*?分钟', '', content)
        content = re.sub(r'字数[:：]\d+', '', content)
        
        # 移除日期标记和时间戳
        content = re.sub(r'\d{4}[\.年/-]\d{1,2}[\.月/-]\d{1,2}[日号]?(\s+\d{1,2}:\d{1,2}(:\d{1,2})?)?', '', content)
        
        # 移除分隔线
        content = re.sub(r'\*\s*\*\s*\*', '', content)
        content = re.sub(r'-{3,}', '', content)
        content = re.sub(r'_{3,}', '', content)
        
        # 移除网页中常见的广告标记
        ad_patterns = [
            r'广告\s*[\.。]?', 
            r'赞助内容', 
            r'sponsored content',
            r'advertisement',
            r'promoted content',
            r'推广信息',
            r'\[广告\]',
            r'【广告】',
        ]
        for pattern in ad_patterns:
            content = re.sub(pattern, '', content, flags=re.IGNORECASE)
        
        # 移除URL链接和空的Markdown链接
        content = re.sub(r'https?://\S+', '', content)
        content = re.sub(r'www\.\S+', '', content)
        content = re.sub(r'\[\]\(.*?\)', '', content)  # 空链接引用 [](...)
        content = re.sub(r'\[.+?\]\(\s*\)', '', content)  # 有文本无链接 [text]()
        
        # 清理Markdown格式但保留文本内容
        content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)  # 移除加粗标记但保留内容
        content = re.sub(r'\*(.+?)\*', r'\1', content)      # 移除斜体标记但保留内容
        content = re.sub(r'`(.+?)`', r'\1', content)        # 移除代码标记但保留内容
        
        # 清理文章尾部的"微信编辑"和"推荐阅读"等无关内容
        content = re.sub(r'\*\*微信编辑\*\*.*?$', '', content, flags=re.MULTILINE)
        content = re.sub(r'\*\*推荐阅读\*\*.*?$', '', content, flags=re.MULTILINE | re.DOTALL)
        
        # 清理多余的空白字符
        content = re.sub(r'\n{3,}', '\n\n', content)  # 移除多余空行
        content = re.sub(r'\s{2,}', ' ', content)     # 移除多余空格
        content = re.sub(r'^\s+', '', content, flags=re.MULTILINE)  # 移除行首空白
        content = re.sub(r'\s+$', '', content, flags=re.MULTILINE)  # 移除行尾空白
        
        # 记录清洗后长度
        cleaned_length = len(content)
        logger.debug(f"[JinaSum] Cleaned content length: {cleaned_length}, removed {original_length - cleaned_length} characters")
        
        return content