# encoding:utf-8
import json
import os
import html
import re
from urllib.parse import urlparse
import time

import requests

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *

@plugins.register(
    name="JinaSum",
    desire_priority=20,
    hidden=False,
    desc="Sum url link content with jina reader and llm",
    version="1.1.1",
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
        "jina_reader_base": "https://r.jina.ai",
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
        super().__init__()
        try:
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()
            
            # 使用默认配置初始化
            for key, default_value in self.DEFAULT_CONFIG.items():
                setattr(self, key, self.config.get(key, default_value))
            
            # 每次启动时重置缓存
            self.pending_messages = {}  # 待处理消息缓存
            
            logger.info(f"[JinaSum] inited, config={self.config}")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{e}")
            raise "[JinaSum] init failed, ignore "

    def on_handle_context(self, e_context: EventContext):
        """处理消息"""
        context = e_context['context']
        if context.type not in [ContextType.TEXT, ContextType.SHARING]:
            return

        content = context.content
        channel = e_context['channel']
        msg = e_context['context']['msg']
        chat_id = msg.from_user_id
        is_group = msg.is_group

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
                    return self._process_summary(cached_content, e_context, retry_count=0, skip_notice=True)
                
                # 检查是否是直接URL总结，移除"总结"并检查剩余内容是否为URL
                url = content.replace("总结", "").strip()
                if url and self._check_url(url):
                    logger.debug(f"[JinaSum] Processing direct URL: {url}")
                    return self._process_summary(url, e_context, retry_count=0)
                logger.debug("[JinaSum] No content to summarize")
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

    def _process_summary(self, content: str, e_context: EventContext, retry_count: int = 0, skip_notice: bool = False):
        """处理总结请求
        
        Args:
            content: 要处理的内容
            e_context: 事件上下文
            retry_count: 重试次数
            skip_notice: 是否跳过提示消息
        """
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
            jina_url = self._get_jina_url(target_url)
            logger.debug(f"[JinaSum] Requesting jina url: {jina_url}")
            
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
            try:
                response = requests.get(jina_url, headers=headers, timeout=60)
                response.raise_for_status()
                target_url_content = response.text
                if not target_url_content:
                    raise ValueError("Empty response from jina reader")
            except Exception as e:
                logger.error(f"[JinaSum] Failed to get content from jina reader: {str(e)}")
                raise
            
            # 清洗内容，去除图片、链接、广告等无用信息
            target_url_content = self._clean_content(target_url_content)
            
            # 限制内容长度
            target_url_content = target_url_content[:self.max_words]
            logger.debug(f"[JinaSum] Got content length: {len(target_url_content)}")
            
            # 构造提示词和内容
            sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
            
            # 修改 context 内容，传递给下一个插件处理
            e_context['context'].type = ContextType.TEXT
            e_context['context'].content = sum_prompt
            
            try:
                # 确保设置一个默认的 reply，以防后续插件没有设置
                default_reply = Reply(ReplyType.TEXT, "抱歉，处理过程中出现错误")
                e_context["reply"] = default_reply
                
                # 继续传递给下一个插件处理
                e_context.action = EventAction.CONTINUE
                logger.debug(f"[JinaSum] Passing content to next plugin: length={len(sum_prompt)}")
                return
                
            except Exception as e:
                logger.warning(f"[JinaSum] Failed to handle context: {str(e)}")
                # 如果出错，确保有一个 reply
                error_reply = Reply(ReplyType.ERROR, "处理过程中出现错误")
                e_context["reply"] = error_reply
                e_context.action = EventAction.BREAK_PASS
            
        except Exception as e:
            logger.error(f"[JinaSum] Error in processing summary: {str(e)}", exc_info=True)
            if retry_count < 3:
                logger.info(f"[JinaSum] Retrying {retry_count + 1}/3...")
                return self._process_summary(content, e_context, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"无法获取该内容: {str(e)}")
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
        logger.debug("No Suno plugin config.json, use plugins/jina_sum/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _get_jina_url(self, target_url):
        return self.jina_reader_base + "/" + target_url

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"

    def _get_openai_headers(self):
        return {
            'Authorization': f"Bearer {self.open_ai_api_key}",
            'Host': urlparse(self.open_ai_api_base).netloc
        }

    def _get_openai_payload(self, target_url_content):
        target_url_content = target_url_content[:self.max_words] # 通过字符串长度简单行截
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
        # 简单校验是否是url
        if not stripped_url.startswith("http://") and not stripped_url.startswith("https://"):
            return False

        # 检查白名单
        if len(self.white_url_list):
            if not any(stripped_url.startswith(white_url) for white_url in self.white_url_list):
                return False

        # 排除黑名单，黑名单优先级>白名单
        for black_url in self.black_url_list:
            if stripped_url.startswith(black_url):
                return False

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