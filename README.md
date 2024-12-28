# jina_sumary
ChatGPT on WeChat项目插件, 使用jina reader和ChatGPT总结网页链接内容

支持总结公众号、小红书、csdn等分享卡片链接(有的卡片链接会触发验证，一般直链没有此问题)

## 功能
- 支持自动总结微信文章
- 支持手动触发总结
- 支持总结后追问文章内容
- 支持群聊和私聊场景
- 支持黑名单群组配置

## 使用方法
1. 私聊：
   - 直接发送文章链接或分享卡片，会自动总结
   - 总结完成后5分钟内可发送"问xxx"追问文章内容

2. 群聊：
   - 当auto_sum=true时：
     - 非黑名单群组：自动总结分享卡片和链接
     - 黑名单群组：需要发送"总结"触发总结
   - 当auto_sum=false时：
     - 所有群组都需要发送"总结"触发总结
   - 分享卡片总结：
     - 发送卡片后，发送"总结"触发
   - URL总结方式灵活，支持：
     - "总结 链接"
     - "总结链接"
     - "链接总结"
   - 总结完成后5分钟内可发送"问xxx"追问文章内容

![wechat_mp](./docs/images/wechat_mp.jpg)
![red](./docs/images/red.jpg)
![csdn](./docs/images/csdn.jpg)

## 配置说明
```json
{
    "jina_reader_base": "https://r.jina.ai",          # jina reader链接，默认为https://r.jina.ai
    "open_ai_api_base": "https://api.openai.com/v1",  # chatgpt chat url
    "open_ai_api_key": "sk-xxx",                      # chatgpt api key
    "open_ai_model": "gpt-3.5-turbo",                 # chatgpt model
    "max_words": 8000,                                # 网页链接内容的最大字数，防止超过最大输入token
    "auto_sum": false,                                # 是否自动总结（仅群聊有效）
    "white_url_list": [],                             # url白名单, 列表为空时不做限制，黑名单优先级大于白名单
    "black_url_list": [                               # url黑名单，排除不支持总结的视频号等链接
        "https://support.weixin.qq.com",
        "https://channels-aladin.wxqcloud.qq.com"
    ],
    "black_group_list": [],                           # 群聊黑名单，使用群名
    "prompt": "我需要对下面的文本进行总结，总结输出包括以下三个部分：\n📖 一句话总结\n🔑 关键要点,用数字序号列出3-5个文章的核心内容\n🏷 标签: #xx #xx\n请使用emoji让你的表达更生动。"  # 链接内容总结提示词
}
```

## 注意事项
1. 需要配置 OpenAI API Key
2. 群聊中需要@机器人触发总结
3. 追问功能仅在总结完成后5分钟内有效
4. 支持的文章来源：微信公众号、知乎、简书等主流平台
5. 黑名单群组配置使用群名，不是群ID
6. 私聊中始终自动总结，不受auto_sum配置影响

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=hanfangyuan4396/jina_sum&type=Date)](https://star-history.com/#hanfangyuan4396/jina_sum&Date)
