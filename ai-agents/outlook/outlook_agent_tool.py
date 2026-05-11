import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import uuid
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import mimetypes
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
class OutlookAgentTool(ConnectedServiceToolAgent):

    async def run(self, *args, **kwargs):
        
        if not self.access_token:
            yield {"type": "final", "content": "Please connect your Microsoft account in the UI first."}
            return

        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"Authorization": f"Bearer {self.access_token}"}

            resp = await client.get(
                "https://graph.microsoft.com/v1.0/me/messages",
                headers=headers
            )

            if resp.status_code == 401:
                await self.refresh_access_token(client=client)
                headers["Authorization"] = f"Bearer {self.access_token}"
                resp = await client.get(
                    "https://graph.microsoft.com/v1.0/me/messages",
                    headers=headers
                )

            messages = resp.json().get("value", [])
            yield {"type": "final", "content": messages}
