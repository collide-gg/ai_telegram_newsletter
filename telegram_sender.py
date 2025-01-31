"""Telegram message formatting and sending service"""

import logging
import html
import telegram
from telegram import Bot
from telegram.constants import ParseMode
import asyncio
import os
from pathlib import Path
import json
from datetime import datetime
from dotenv import load_dotenv
import re
import sys
from error_handler import RetryConfig, with_retry, TelegramError, log_error, DataProcessingError
from category_mapping import TELEGRAM_CHANNEL_MAP, EMOJI_MAP
import time

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce httpx logging
logging.getLogger('httpx').setLevel(logging.WARNING)

class TelegramSender:
    def __init__(self, bot_token):
        if not bot_token:
            raise ValueError("Bot token is required")
        self.bot = Bot(token=bot_token)
        
    async def format_text(self, text):
        """Format text with HTML tags according to instructions"""
        if not text:
            logger.warning("Received empty text to format")
            return ""
            
        # Skip if text is already formatted (contains HTML tags)
        if any(tag in text for tag in ['<u>', '<b>', '<i>', '<a href']):
            return text
            
        try:
            lines = text.split('\n')
            formatted_lines = []
            
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue
                    
                # Format header (Date - Category Rollup)
                if ' - ' in line and 'Rollup' in line:
                    try:
                        date_str, rest = line.split(' - ', 1)
                        date_obj = datetime.strptime(date_str.strip(), '%Y%m%d')
                        formatted_date = date_obj.strftime('%B %d')
                        formatted_header = f"{formatted_date} - {rest}"
                        formatted_lines.append(f"<u><b><i>{html.escape(formatted_header)}</i></b></u>")
                    except ValueError as e:
                        log_error(logger, e, f"Failed to parse date: {date_str}")
                        # Fallback to original format if date conversion fails
                        formatted_lines.append(f"<u><b><i>{html.escape(line)}</i></b></u>")
                    i += 1
                    continue
                    
                # Format subcategory with emoji
                if not ':' in line and not line.startswith('http'):
                    if formatted_lines:
                        formatted_lines.append('')
                    formatted_lines.append(f"<u><b>{html.escape(line)}</b></u>")
                    i += 1
                    continue
                    
                # Format tweet lines (author: text) with URL on next line
                if ':' in line and not line.startswith('http'):
                    try:
                        author, content = line.split(':', 1)
                        url = ""
                        
                        # Check next line for URL
                        if i + 1 < len(lines) and lines[i + 1].strip().startswith('http'):
                            url = lines[i + 1].strip()
                            i += 2
                        else:
                            i += 1
                            
                        formatted_lines.append(f"- <b>{html.escape(author.strip())}</b>: <a href='{url}'>{html.escape(content.strip())}</a>")
                            
                    except Exception as e:
                        log_error(logger, e, f"Failed to format tweet line: {line}")
                        formatted_lines.append(line)
                        i += 1
                    continue
                    
                # Keep URLs as is but prevent auto-embedding
                if line.startswith('http'):
                    formatted_lines.append(line.replace('https:', 'https:\u200B'))
                    i += 1
                    continue
                    
                # Default case - keep line as is
                formatted_lines.append(html.escape(line))
                i += 1
                
            return '\n'.join(formatted_lines)
            
        except Exception as e:
            log_error(logger, e, "Failed to format text")
            raise TelegramError(f"Text formatting failed: {str(e)}")

    @with_retry(RetryConfig(max_retries=3, base_delay=1.0))
    async def send_message(self, channel_id: str, text: str) -> bool:
        """Send a message to a Telegram channel with retry logic"""
        if not text:
            return False
            
        if not channel_id:
            logger.error("No channel ID provided")
            return False
            
        try:
            formatted_text = await self.format_text(text)
            if not formatted_text:
                logger.error("Empty formatted text")
                return False
                
            await self.bot.send_message(
                chat_id=channel_id,
                text=formatted_text,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            return True
            
        except Exception as e:
            log_error(logger, e, f"Failed to send message to channel {channel_id}")
            raise TelegramError(f"Failed to send message: {str(e)}")

    async def process_category(self, category: str, content: dict):
        """Process and send a category summary to Telegram"""
        try:
            channel_id = TELEGRAM_CHANNEL_MAP[category]
            if not await self._validate_channel(channel_id):
                logger.error(f"Skipping invalid channel: {channel_id}")
                return False
            
            formatted_text = await self.format_text(content['text'])
            if not formatted_text:
                logger.error(f"Empty content for {category}")
                return False
            
            return await self.send_message(channel_id, formatted_text)
        
        except KeyError as e:
            logger.error(f"Category {category} not found in channel map")
            return False
        except Exception as e:
            log_error(logger, e, f"Error processing {category}")
            return False

    async def _validate_channel(self, channel_id: str) -> bool:
        try:
            chat = await self.bot.get_chat(chat_id=channel_id)
            return chat.type in ["channel", "supergroup"]
        except telegram.error.BadRequest:
            logger.error(f"Invalid channel ID: {channel_id}")
            return False

    async def format_category_summary(self, category: str, summary: dict) -> str:
        """Format category summary into a Telegram message"""
        try:
            # Case-insensitive check for category
            category_key = next(
                (k for k in summary.keys() if k.upper() == category),
                None
            )
            
            if not summary or not category_key:
                logger.error(f"Invalid summary format for {category}")
                return ""
            
            category_data = summary[category_key]
            if not isinstance(category_data, dict):
                logger.error(f"Invalid category data format for {category}")
                return ""
            
            # Build message with header
            lines = [f"<u><b><i>{category} Rollup</i></b></u> ~\n"]
            
            # Add each subcategory and its tweets
            for subcategory, tweets in category_data.items():
                # Get emoji for subcategory if it exists
                emoji = EMOJI_MAP.get(subcategory, '')
                
                # Add subcategory header with emoji
                subcategory_text = f"<u><b>{subcategory}</b></u>"
                if emoji:
                    subcategory_text += f" {emoji}"
                lines.append(subcategory_text)
                
                # Group tweets by author
                author_tweets = {}
                for tweet in tweets:
                    author = tweet.get('author', '')
                    text = tweet.get('text', '')
                    url = tweet.get('url', '')
                    
                    if author and text and url:
                        if author not in author_tweets:
                            author_tweets[author] = []
                        author_tweets[author].append((text, url))
                
                # Format consolidated tweets for each author
                for author, tweet_list in author_tweets.items():
                    if len(tweet_list) == 1:
                        # Single tweet format
                        text, url = tweet_list[0]
                        lines.append(f"- <b>{author}</b>: <a href='{url}'>{html.escape(text)}</a>")
                    else:
                        # Multiple tweets consolidated format
                        consolidated = []
                        for text, url in tweet_list:
                            # Create short summary (first part of text up to a sensible break)
                            summary = text.split('.')[0].split(';')[0].strip()
                            if len(summary) > 50:  # Truncate if too long
                                summary = summary[:47] + "..."
                            consolidated.append(f"<a href='{url}'>{html.escape(summary)}</a>")
                        
                        lines.append(f"- <b>{author}</b>: {' • '.join(consolidated)}")
                
                lines.append("")  # Empty line between subcategories
            
            return "\n".join(lines)
            
        except Exception as e:
            log_error(logger, e, f"Failed to format {category} summary")
            return ""

    async def process_news_summary(self, summary_file: Path):
        """Process a news summary file and send to test channel"""
        try:
            # Load and validate summary
            data = await load_json_file(summary_file)
            if not data:
                logger.error(f"Empty summary file: {summary_file}")
                return False
            
            # Get category from filename (e.g., "sei_summary.json" -> "SEI")
            category = summary_file.stem.split('_')[0].upper()
            if not category:
                logger.error(f"Could not determine category from filename: {summary_file}")
                return False
            
            # Use test channel ID
            channel_id = os.getenv('TELEGRAM_TEST_CHANNEL_ID')
            if not channel_id:
                logger.error("Test channel ID not found in environment variables")
                return False
            
            # Format summary
            formatted_text = await self.format_category_summary(category, data)
            if not formatted_text:
                logger.error(f"Failed to format summary for {category}")
                return False
            
            # Format and send message
            html_text = await self.format_text(formatted_text)
            success = await self.send_message(channel_id, html_text)
            
            if success:
                logger.info(f"✅ Successfully sent {category} summary to test channel")
            else:
                logger.error(f"❌ Failed to send {category} summary to test channel")
            
            return success
            
        except Exception as e:
            log_error(logger, e, f"Failed to process summary file: {summary_file}")
            return False

@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def load_json_file(file_path):
    """Load and parse JSON file with retry"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log_error(logger, e, f"Failed to load JSON file: {file_path}")
        raise DataProcessingError(f"Failed to load JSON file: {str(e)}")

@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def process_category(sender, category, content, channel_id):
    """Process and send a category summary with retry"""
    try:
        if not isinstance(content, dict) or 'text' not in content:
            logger.error(f"Invalid content structure for {category}")
            return False
            
        raw_text = content['text']
        if not raw_text:
            logger.error(f"Empty text for {category}")
            return False
            
        formatted_text = await sender.format_text(raw_text)
        if not formatted_text:
            logger.error(f"Empty formatted text for {category}")
            return False
            
        return await sender.send_message(channel_id=channel_id, text=formatted_text)
        
    except Exception as e:
        log_error(logger, e, f"Failed to process category: {category}")
        raise DataProcessingError(f"Failed to process category: {str(e)}")

if __name__ == "__main__":
    load_dotenv()
    
    try:
        # Initialize sender with bot token
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN not found in environment")
            
        sender = TelegramSender(bot_token)
        
        # Process all summary files
        summary_dir = Path('data/filtered/news_filtered')
        summary_files = list(summary_dir.glob('*_summary.json'))
        
        if not summary_files:
            logger.warning("No summary files found to process")
            sys.exit(0)
            
        logger.info(f"Found {len(summary_files)} summary files to process")
        
        async def process_all_files():
            for summary_file in summary_files:
                try:
                    logger.info(f"Processing {summary_file.name}")
                    await sender.process_news_summary(summary_file)
                    # Brief pause between messages
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"Failed to process {summary_file.name}: {str(e)}")
                    continue
        
        # Run everything in a single event loop
        asyncio.run(process_all_files())
                
    except Exception as e:
        logger.error(f"Script error: {str(e)}")
        sys.exit(1) 