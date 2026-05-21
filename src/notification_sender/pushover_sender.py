# -*- coding: utf-8 -*-
"""
Pushover 发送提醒服务

职责：
1. 通过 Pushover API 发送 Pushover 消息
"""
import logging
import os
import tempfile
from typing import Optional
from datetime import datetime
import requests

from src.config import Config
from src.formatters import markdown_to_plain_text


logger = logging.getLogger(__name__)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
    line-height: 1.55;
    color: #1a1a1a;
    background: #ffffff;
    padding: 16px 18px;
    max-width: 760px;
}}
h1 {{
    font-size: 17px;
    color: #1a1a2e;
    border-bottom: 3px solid #4f46e5;
    padding-bottom: 6px;
    margin-bottom: 14px;
}}
h2 {{
    font-size: 13px;
    font-weight: 700;
    color: #fff;
    background: #4f46e5;
    padding: 4px 10px;
    border-radius: 4px;
    margin: 14px 0 5px;
}}
h3 {{
    font-size: 13px;
    color: #333;
    margin: 8px 0 3px;
}}
hr {{
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 10px 0;
}}
strong {{ color: #1a1a2e; }}
em {{ color: #555; }}
code {{
    background: #f4f4f4;
    padding: 1px 4px;
    border-radius: 2px;
    font-size: 11px;
    font-family: 'Courier New', monospace;
}}
pre {{
    background: #f4f4f4;
    padding: 8px;
    border-radius: 4px;
    font-size: 11px;
    overflow: hidden;
    white-space: pre-wrap;
}}
p {{ margin: 3px 0; }}
ul, ol {{ margin: 4px 0; padding-left: 18px; }}
li {{ margin: 2px 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 12px; }}
th {{ background: #e8e8f8; padding: 4px 8px; text-align: left; border: 1px solid #ccc; }}
td {{ padding: 3px 8px; border: 1px solid #e0e0e0; }}
blockquote {{
    border-left: 3px solid #4f46e5;
    margin: 6px 0;
    padding: 4px 10px;
    color: #555;
    background: #f8f8ff;
}}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>"""


class PushoverSender:

    def __init__(self, config: Config):
        self._pushover_config = {
            'user_key': getattr(config, 'pushover_user_key', None),
            'api_token': getattr(config, 'pushover_api_token', None),
        }

    def _is_pushover_configured(self) -> bool:
        return bool(self._pushover_config['user_key'] and self._pushover_config['api_token'])

    def send_to_pushover(
        self,
        content: str,
        title: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        if not self._is_pushover_configured():
            logger.warning("Pushover 配置不完整，跳过推送")
            return False

        user_key = self._pushover_config['user_key']
        api_token = self._pushover_config['api_token']
        api_url = "https://api.pushover.net/1/messages.json"

        if title is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
            title = f"📈 股票分析报告 - {date_str}"

        max_length = 1024
        plain_content = markdown_to_plain_text(content)

        if len(plain_content) <= max_length:
            return self._send_pushover_message(
                api_url, user_key, api_token, plain_content, title,
                timeout_seconds=timeout_seconds,
            )

        # Try image attachment for long reports
        image_path = self._generate_report_image(content, title)
        if image_path:
            summary = plain_content[:300].strip()
            if len(plain_content) > 300:
                summary += " ..."
            return self._send_pushover_with_image(
                api_url, user_key, api_token, image_path, title, summary,
                timeout_seconds=timeout_seconds,
            )

        # Fallback: chunked text
        return self._send_pushover_chunked(
            api_url, user_key, api_token, plain_content, title, max_length,
            timeout_seconds=timeout_seconds,
        )

    def _generate_report_image(self, content: str, title: str) -> Optional[str]:
        """Convert markdown report to JPEG image. Returns temp file path or None on failure."""
        try:
            import imgkit
            import markdown2

            html_body = markdown2.markdown(
                content,
                extras=['fenced-code-blocks', 'tables', 'strike', 'break-on-newline'],
            )
            html = _HTML_TEMPLATE.format(title=title, body=html_body)

            tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            tmp.close()

            options = {
                'format': 'jpg',
                'quality': '72',
                'width': '760',
                'quiet': '',
                'encoding': 'UTF-8',
                'disable-smart-width': '',
                'no-stop-slow-scripts': '',
                'load-error-handling': 'ignore',
            }

            imgkit.from_string(html, tmp.name, options=options)

            # Compress if over Pushover 2.5 MB limit
            size = os.path.getsize(tmp.name)
            if size > 2_400_000:
                self._compress_image(tmp.name, size)

            logger.info(f"报告图片已生成: {os.path.getsize(tmp.name) // 1024} KB")
            return tmp.name

        except Exception as e:
            logger.warning(f"生成报告图片失败: {e}，回退到文本分批发送")
            return None

    def _compress_image(self, path: str, original_size: int) -> None:
        """Compress image file in-place using Pillow if available."""
        try:
            from PIL import Image

            img = Image.open(path)
            # Calculate quality needed to fit under 2.4 MB
            quality = max(30, int(72 * (2_400_000 / original_size) ** 0.8))
            img.save(path, 'JPEG', quality=quality, optimize=True)

            new_size = os.path.getsize(path)
            if new_size > 2_400_000:
                # Scale dimensions down
                ratio = (2_400_000 / new_size) ** 0.5
                new_w = int(img.width * ratio)
                new_h = int(img.height * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                img.save(path, 'JPEG', quality=quality, optimize=True)

        except ImportError:
            logger.debug("Pillow 未安装，跳过图片压缩")
        except Exception as e:
            logger.debug(f"图片压缩失败: {e}")

    def _send_pushover_with_image(
        self,
        api_url: str,
        user_key: str,
        api_token: str,
        image_path: str,
        title: str,
        summary: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Send a Pushover notification with an image attachment."""
        try:
            with open(image_path, 'rb') as f:
                files = {'attachment': ('report.jpg', f, 'image/jpeg')}
                payload = {
                    'token': api_token,
                    'user': user_key,
                    'message': summary,
                    'title': title,
                }
                response = requests.post(
                    api_url, data=payload, files=files,
                    timeout=timeout_seconds or 60,
                )

            if response.status_code == 200 and response.json().get('status') == 1:
                logger.info("Pushover 图片消息发送成功")
                return True
            else:
                logger.error(f"Pushover 图片消息发送失败: {response.text}")
                return False

        except Exception as e:
            logger.error(f"发送 Pushover 图片消息失败: {e}")
            return False
        finally:
            try:
                os.unlink(image_path)
            except Exception:
                pass

    def _send_pushover_message(
        self,
        api_url: str,
        user_key: str,
        api_token: str,
        message: str,
        title: str,
        priority: int = 0,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        try:
            payload = {
                "token": api_token,
                "user": user_key,
                "message": message,
                "title": title,
                "priority": priority,
            }
            response = requests.post(api_url, data=payload, timeout=timeout_seconds or 30)

            if response.status_code == 200:
                result = response.json()
                if result.get('status') == 1:
                    logger.info("Pushover 消息发送成功")
                    return True
                else:
                    errors = result.get('errors', ['未知错误'])
                    logger.error(f"Pushover 返回错误: {errors}")
                    return False
            else:
                logger.error(f"Pushover 请求失败: HTTP {response.status_code}")
                logger.debug(f"响应内容: {response.text}")
                return False

        except Exception as e:
            logger.error(f"发送 Pushover 消息失败: {e}")
            return False

    def _send_pushover_chunked(
        self,
        api_url: str,
        user_key: str,
        api_token: str,
        content: str,
        title: str,
        max_length: int,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        import time

        if "────────" in content:
            sections = content.split("────────")
            separator = "────────"
        else:
            sections = content.split("\n\n")
            separator = "\n\n"

        chunks = []
        current_chunk = []
        current_length = 0

        for section in sections:
            if current_chunk:
                new_length = current_length + len(separator) + len(section)
            else:
                new_length = len(section)

            if new_length > max_length:
                if current_chunk:
                    chunks.append(separator.join(current_chunk))
                current_chunk = [section]
                current_length = len(section)
            else:
                current_chunk.append(section)
                current_length = new_length

        if current_chunk:
            chunks.append(separator.join(current_chunk))

        total_chunks = len(chunks)
        success_count = 0

        logger.info(f"Pushover 分批发送：共 {total_chunks} 批")

        for i, chunk in enumerate(chunks):
            chunk_title = f"{title} ({i+1}/{total_chunks})" if total_chunks > 1 else title

            if self._send_pushover_message(
                api_url, user_key, api_token, chunk, chunk_title,
                timeout_seconds=timeout_seconds,
            ):
                success_count += 1
                logger.info(f"Pushover 第 {i+1}/{total_chunks} 批发送成功")
            else:
                logger.error(f"Pushover 第 {i+1}/{total_chunks} 批发送失败")

            if i < total_chunks - 1:
                time.sleep(1)

        return success_count == total_chunks
