import asyncio
import os
import sys
import html
import time
import json
import hmac
import hashlib
import random
import string
import secrets
import re
import unicodedata
import urllib.request
import urllib.error
from collections import defaultdict, deque
from typing import Set, Dict, Any, Optional
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse
import uvicorn

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class AnnouncementHTMLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        from starlette.responses import Response
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return Response(content=body, status_code=response.status_code,
                             headers=dict(response.headers), media_type=response.media_type)
        async with announcement_lock:
            msg = announcement_text
        if msg and "</body>" in text:
            aid = hashlib.sha256(msg.encode("utf-8")).hexdigest()[:16]
            safe = html.escape(msg)
            widget = (
                '<div id="dn-site-announcement" data-id="'+aid+'" role="status">'
                '<div class="dn-ann-icon">!</div><div class="dn-ann-copy"><span>DEXNOTIFIER</span><strong>'+safe+'</strong></div>'
                '<button id="dn-ann-close" type="button" aria-label="Dismiss">&times;</button></div>'
                '<style>#dn-site-announcement{position:fixed;z-index:999999;top:18px;left:50%;transform:translate(-50%,-18px);width:min(calc(100% - 24px),900px);display:flex;align-items:center;gap:13px;padding:13px 14px;border:1px solid rgba(129,140,248,.32);border-radius:18px;background:rgba(8,10,16,.90);box-shadow:0 24px 80px rgba(0,0,0,.48),0 0 50px rgba(99,102,241,.12);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);color:#fff;font-family:Inter,system-ui,sans-serif;opacity:0;animation:dnAnnIn .38s ease .05s forwards}.dn-ann-icon{width:34px;height:34px;flex:0 0 34px;border-radius:11px;display:grid;place-items:center;background:linear-gradient(135deg,#6366f1,#22d3ee);font-weight:900}.dn-ann-copy{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}.dn-ann-copy span{font-size:9px;letter-spacing:.18em;font-weight:900;color:#9ca3ff}.dn-ann-copy strong{font-size:13px;line-height:1.45;color:#f8fafc;overflow-wrap:anywhere}#dn-ann-close{width:34px;height:34px;flex:0 0 34px;border:1px solid rgba(255,255,255,.10);border-radius:10px;background:rgba(255,255,255,.05);color:#cbd5e1;font-size:21px;cursor:pointer;transition:.18s ease}#dn-ann-close:hover{color:#fff;background:rgba(255,255,255,.10);transform:scale(1.05)}@keyframes dnAnnIn{to{opacity:1;transform:translate(-50%,0)}}#dn-site-announcement.dn-hide{animation:dnAnnOut .22s ease forwards}@keyframes dnAnnOut{to{opacity:0;transform:translate(-50%,-12px)}}@media(max-width:640px){#dn-site-announcement{top:10px;width:calc(100% - 16px);padding:10px;border-radius:15px}.dn-ann-icon{width:30px;height:30px;flex-basis:30px}.dn-ann-copy strong{font-size:12px}#dn-ann-close{width:30px;height:30px;flex-basis:30px}}}</style>'
                '<script>(()=>{const e=document.getElementById("dn-site-announcement");if(!e)return;const k="dn-announcement-dismissed:"+e.dataset.id;if(localStorage.getItem(k)==="1"){e.remove();return}document.getElementById("dn-ann-close").onclick=()=>{e.classList.add("dn-hide");localStorage.setItem(k,"1");setTimeout(()=>e.remove(),240)}})();</script>'
            )
            text = text.replace("</body>", widget + "</body>", 1)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=text.encode("utf-8"), status_code=response.status_code, headers=headers, media_type=response.media_type)

app.add_middleware(AnnouncementHTMLMiddleware)
START_TIME = time.time()


GLOBAL_UI_CSS = r"""
:root{
  --dn-bg:#050608;--dn-bg-soft:#090b10;--dn-surface:rgba(14,16,22,.86);--dn-surface-2:rgba(18,21,29,.78);
  --dn-line:rgba(255,255,255,.075);--dn-line-strong:rgba(255,255,255,.13);
  --dn-text:#f7f7fb;--dn-muted:#8f98a9;--dn-dim:#606a7c;
  --dn-purple:#8b5cf6;--dn-indigo:#6366f1;--dn-cyan:#22d3ee;--dn-green:#34d399;--dn-red:#fb7185;
  --dn-shadow:0 28px 90px rgba(0,0,0,.52);--dn-radius:22px
}
*{box-sizing:border-box}
html{background:#050608!important;scroll-behavior:smooth}
body{position:relative;overflow-x:hidden!important;min-height:100vh!important;background:
 radial-gradient(850px 520px at 8% -12%,rgba(99,102,241,.13),transparent 65%),
 radial-gradient(700px 460px at 96% 0%,rgba(34,211,238,.055),transparent 62%),
 linear-gradient(180deg,#050608 0%,#07090d 52%,#050608 100%)!important;
 color:var(--dn-text)!important;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif!important}
body:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:-2;background-image:linear-gradient(rgba(255,255,255,.022) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px);background-size:64px 64px;mask-image:linear-gradient(to bottom,#000 0%,rgba(0,0,0,.55) 55%,transparent 100%);opacity:.55}
body:after{content:"";position:fixed;left:var(--dn-mx,50%);top:var(--dn-my,20%);width:520px;height:520px;transform:translate(-50%,-50%);border-radius:50%;pointer-events:none;z-index:-1;background:radial-gradient(circle,rgba(139,92,246,.09),transparent 67%);filter:blur(22px);transition:left .22s ease,top .22s ease}
body.dn-base-home,html:has(body.dn-base-home){background:#000!important}
body.dn-base-home:before,body.dn-base-home:after{display:none!important}
.dn-chrome{position:relative;z-index:50;width:min(1220px,calc(100% - 34px));margin:18px auto 28px;padding:10px 12px;border:1px solid rgba(255,255,255,.075);border-radius:18px;background:rgba(9,11,16,.72);box-shadow:0 18px 55px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.035);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);display:flex;align-items:center;justify-content:space-between;gap:16px;animation:dnSlide .55s cubic-bezier(.2,.8,.2,1) both}
.dn-chrome-brand{display:flex;align-items:center;gap:10px;color:#fff!important;font-weight:950!important;letter-spacing:-.03em;font-size:14px}
.dn-chrome-logo{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;background:linear-gradient(135deg,#8b5cf6,#4f8cff);box-shadow:0 9px 28px rgba(99,102,241,.24);font-size:13px;font-weight:1000;color:#fff;position:relative;overflow:hidden}
.dn-chrome-logo:after{content:"";position:absolute;inset:-80%;background:linear-gradient(120deg,transparent 35%,rgba(255,255,255,.35),transparent 65%);animation:dnShine 4s ease-in-out infinite}
.dn-chrome-links{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.dn-chrome-links a{padding:8px 11px!important;border-radius:10px!important;color:#9da7b9!important;font-size:12px!important;font-weight:800!important;border:1px solid transparent!important;background:transparent!important}
.dn-chrome-links a:hover{color:#fff!important;background:rgba(255,255,255,.055)!important;border-color:rgba(255,255,255,.07)!important}
.dn-chrome-links a.active{color:#fff!important;background:rgba(139,92,246,.12)!important;border-color:rgba(139,92,246,.22)!important}
.dn-chrome-status{display:inline-flex;align-items:center;gap:7px;color:#9ee8c7;font-size:11px;font-weight:850;padding:7px 10px;border-radius:999px;border:1px solid rgba(52,211,153,.15);background:rgba(52,211,153,.055)}
.dn-chrome-status i{width:6px;height:6px;border-radius:50%;background:#34d399;box-shadow:0 0 14px rgba(52,211,153,.9);animation:dnPulse 1.8s ease-in-out infinite}
.wrap,.container,.page,.shell{position:relative;z-index:1}
.wrap{animation:dnPageIn .62s cubic-bezier(.2,.8,.2,1) both}
.card,.panel,.stat-box,.script-card,.resultbox,.logs-box,.locked-note,.code-box{background:linear-gradient(145deg,rgba(17,20,27,.88),rgba(8,10,14,.9))!important;border:1px solid var(--dn-line)!important;box-shadow:0 20px 65px rgba(0,0,0,.28),inset 0 1px 0 rgba(255,255,255,.035)!important;backdrop-filter:blur(18px)!important;-webkit-backdrop-filter:blur(18px)!important}
.card,.panel,.script-card{border-radius:var(--dn-radius)!important;transition:transform .28s ease,border-color .28s ease,box-shadow .28s ease}
.card:hover,.panel:hover,.script-card:hover{transform:translateY(-3px);border-color:rgba(139,92,246,.27)!important;box-shadow:0 28px 80px rgba(0,0,0,.38),0 0 0 1px rgba(139,92,246,.035)!important}
h1,h2,h3,h4{color:#fff!important;letter-spacing:-.035em!important}
p,.small-text,.tagline,.label,.hint{color:var(--dn-muted)!important}
a{color:#a5b4fc!important;text-decoration:none!important;transition:color .2s ease,background .2s ease,border-color .2s ease,transform .2s ease}
a:hover{color:#67e8f9!important}
input,textarea,select,.editor,.out{background:rgba(3,5,9,.84)!important;color:#eef2ff!important;border:1px solid rgba(255,255,255,.09)!important;border-radius:14px!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.025)!important;transition:border-color .2s ease,box-shadow .2s ease,transform .2s ease!important}
input:hover,textarea:hover,select:hover{border-color:rgba(255,255,255,.15)!important}
input:focus,textarea:focus,select:focus,.editor:focus{border-color:rgba(139,92,246,.68)!important;box-shadow:0 0 0 4px rgba(139,92,246,.09),0 0 35px rgba(139,92,246,.06)!important;outline:none!important}
button,.btn,.copy,.copy-btn{position:relative;overflow:hidden;border:1px solid rgba(255,255,255,.08)!important;border-radius:13px!important;background:linear-gradient(135deg,#8b5cf6,#6366f1 55%,#4f8cff)!important;color:#fff!important;box-shadow:0 12px 30px rgba(99,102,241,.18)!important;transition:transform .2s ease,box-shadow .2s ease,filter .2s ease!important;font-weight:850!important}
button:hover,.btn:hover,.copy:hover,.copy-btn:hover{transform:translateY(-2px);filter:brightness(1.07);box-shadow:0 17px 42px rgba(99,102,241,.28)!important}
button:active,.btn:active,.copy:active,.copy-btn:active{transform:translateY(0) scale(.985)}
button:before,.btn:before,.copy:before,.copy-btn:before{content:"";position:absolute;inset:0;transform:translateX(-115%);background:linear-gradient(105deg,transparent 25%,rgba(255,255,255,.2) 48%,transparent 70%);transition:transform .58s ease}
button:hover:before,.btn:hover:before,.copy:hover:before,.copy-btn:hover:before{transform:translateX(115%)}
.pill{border-radius:999px!important;background:rgba(99,102,241,.09)!important;border:1px solid rgba(129,140,248,.2)!important;color:#c7d2fe!important;padding:6px 10px!important}
.pill.green{background:rgba(52,211,153,.07)!important;border-color:rgba(52,211,153,.2)!important;color:#a7f3d0!important}
.pill.red{background:rgba(251,113,133,.07)!important;border-color:rgba(251,113,133,.2)!important;color:#fecdd3!important}
.pill.purple{background:rgba(139,92,246,.08)!important;border-color:rgba(139,92,246,.22)!important;color:#ddd6fe!important}
.stats-grid{gap:14px!important}.stat-box{border-radius:17px!important;padding:16px!important}.stat-value{font-size:25px!important;color:#fff!important}.stat-label{color:var(--dn-muted)!important}
.logs-box{border-radius:15px!important;color:#cbd5e1!important}
.banner{border-radius:17px!important;border:1px solid rgba(139,92,246,.18)!important;background:linear-gradient(90deg,rgba(139,92,246,.075),rgba(34,211,238,.045))!important;box-shadow:0 14px 45px rgba(0,0,0,.2)!important}
footer{color:#586274!important}
body:not(.dn-base-home) .hero h1{letter-spacing:-.055em!important}
body:not(.dn-base-home) .wrap{padding-top:0}
body:not(.dn-base-home) .grid{gap:16px!important}
body:not(.dn-base-home) .script-card::before{opacity:.55}
.dn-home{min-height:100vh!important}
.dn-home-inner{width:min(1180px,100%)!important}
.dn-nav{margin-bottom:84px!important}
.dn-logo{box-shadow:0 14px 44px rgba(99,102,241,.25)!important}
.dn-side,.dn-mini{background:linear-gradient(145deg,rgba(17,19,24,.82),rgba(7,8,11,.92))!important;border-color:rgba(255,255,255,.08)!important}
.dn-side{box-shadow:0 30px 90px rgba(0,0,0,.5)!important}
.dn-mini{transition:transform .25s ease,border-color .25s ease,background .25s ease!important}
.dn-mini:hover{transform:translateY(-4px);border-color:rgba(139,92,246,.22)!important;background:rgba(15,17,22,.92)!important}
body:has(.workspace){background:#050608!important}
body:has(.workspace) .page{max-width:1180px!important}
body:has(.workspace) .nav{border-bottom-color:rgba(255,255,255,.065)!important}
body:has(.workspace) .workspace{box-shadow:0 35px 110px rgba(0,0,0,.45)!important;border-color:rgba(255,255,255,.09)!important}
body:has(.workspace) .editor-card{box-shadow:inset 0 1px 0 rgba(255,255,255,.035)!important}
body:has(.workspace) .go{background:linear-gradient(135deg,#8b5cf6,#6366f1 60%,#22d3ee)!important}
body:has(.stats-grid) .wrap,body:has(form[action="/home"]) .wrap{max-width:1220px!important}
body:has(.stats-grid) h1{font-size:clamp(28px,4vw,42px)!important}
.me-admin-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}.me-admin-form,.me-members{padding:15px;border:1px solid rgba(255,255,255,.065);border-radius:16px;background:rgba(255,255,255,.025)}.me-admin-form label{display:block;color:#cbd5e1;font-size:12px;font-weight:850;margin-bottom:8px}.me-add-row{display:flex;gap:8px}.me-add-row select{flex:1;min-width:0}.me-member{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.055)}.me-member:last-child{border-bottom:0}.me-member strong{display:block;color:#fff;font-size:12px}.me-member span{display:block;color:#687489;font-size:10px;margin-top:3px}.me-remove{background:rgba(251,113,133,.08)!important;color:#fecdd3!important;border-color:rgba(251,113,133,.18)!important;box-shadow:none!important;padding:8px 10px!important;font-size:11px}.me-empty{color:#667286;font-size:12px}@media(max-width:760px){.me-admin-grid{grid-template-columns:1fr}.me-add-row{flex-direction:column}.me-add-row button{width:100%;min-height:44px}}
@media(max-width:760px){
  .dn-chrome{width:calc(100% - 20px);margin:10px auto 18px;padding:9px;border-radius:15px}
  .dn-chrome-links{display:none}.dn-chrome-status{margin-left:auto}
  .wrap{width:min(94%,1100px)!important;padding-left:0!important;padding-right:0!important}
  .card,.panel,.script-card{border-radius:19px!important}.grid{grid-template-columns:1fr!important}.stats-grid{grid-template-columns:1fr 1fr!important}
  .card:hover,.panel:hover,.script-card:hover{transform:none}
}
@media(max-width:460px){.stats-grid{grid-template-columns:1fr!important}.dn-chrome-brand span{display:none}.dn-chrome-logo{width:32px;height:32px}}

.dn-tabbar,.dn-sidebar{display:none}

@media(max-width:1024px){
  .dn-tabbar{
    display:flex;position:fixed;left:0;right:0;bottom:0;z-index:2000;
    justify-content:space-around;align-items:stretch;gap:2px;
    padding:6px 6px calc(6px + env(safe-area-inset-bottom,0px));
    background:rgba(8,10,16,.92);border-top:1px solid rgba(255,255,255,.08);
    backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
    box-shadow:0 -14px 46px rgba(0,0,0,.42)
  }
  .dn-tabbar a,.dn-tabbar button{
    flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
    padding:8px 2px 7px;border-radius:13px;color:#7d8699!important;font-size:10.5px!important;
    font-weight:800!important;text-decoration:none!important;border:none!important;
    background:transparent!important;box-shadow:none!important;font-family:inherit
  }
  .dn-tabbar a svg,.dn-tabbar button svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:2}
  .dn-tabbar a.active,.dn-tabbar button.active{color:#fff!important;background:rgba(139,92,246,.16)!important}
  .dn-tabbar a.active svg,.dn-tabbar button.active svg{stroke:#c4b5fd}
  .dn-tabbar a:active,.dn-tabbar button:active{transform:scale(.94)}
  body.dn-has-tabbar{padding-bottom:calc(70px + env(safe-area-inset-bottom,0px))!important}
  .dn-more-sheet{position:fixed;inset:0;z-index:2100;display:none}
  .dn-more-sheet.open{display:block}
  .dn-more-backdrop{position:absolute;inset:0;background:rgba(3,4,7,.6);backdrop-filter:blur(3px);animation:dnFade .18s ease both}
  .dn-more-panel{position:absolute;left:0;right:0;bottom:0;padding:10px 10px calc(14px + env(safe-area-inset-bottom,0px));
    background:rgba(11,13,19,.97);border-top:1px solid rgba(255,255,255,.09);border-radius:22px 22px 0 0;
    box-shadow:0 -20px 60px rgba(0,0,0,.55);animation:dnSheetUp .22s cubic-bezier(.2,.8,.2,1) both}
  .dn-more-grab{width:36px;height:4px;border-radius:99px;background:rgba(255,255,255,.18);margin:6px auto 12px}
  .dn-more-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .dn-more-grid a{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;
    padding:16px 6px;border-radius:16px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);
    color:#cbd5e1!important;font-size:11.5px!important;font-weight:800!important;text-decoration:none!important}
  .dn-more-grid a svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2}
  .dn-more-grid a.active{color:#fff!important;background:rgba(139,92,246,.14)!important;border-color:rgba(139,92,246,.25)!important}
}

@media(min-width:1025px){
  .dn-sidebar{
    display:flex;flex-direction:column;gap:5px;position:fixed;left:18px;top:50%;
    transform:translateY(-50%);z-index:1500;padding:10px;border-radius:20px;
    background:rgba(9,11,16,.78);border:1px solid rgba(255,255,255,.08);
    box-shadow:0 24px 70px rgba(0,0,0,.45);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
    animation:dnSlide .55s cubic-bezier(.2,.8,.2,1) both
  }
  .dn-sidebar a{
    position:relative;display:flex;align-items:center;justify-content:center;width:46px;height:46px;
    border-radius:14px;color:#8a93a6!important;text-decoration:none!important;transition:.2s ease
  }
  .dn-sidebar a svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:2}
  .dn-sidebar a:hover{color:#fff!important;background:rgba(255,255,255,.06)!important}
  .dn-sidebar a.active{color:#fff!important;background:rgba(139,92,246,.18)!important}
  .dn-sidebar a .dn-tip{
    position:absolute;left:58px;top:50%;transform:translate(-6px,-50%);white-space:nowrap;
    background:#0c0e14;border:1px solid rgba(255,255,255,.1);padding:6px 11px;border-radius:9px;
    font-size:11.5px;font-weight:800;color:#fff!important;opacity:0;pointer-events:none;
    transition:.16s ease;box-shadow:0 10px 30px rgba(0,0,0,.4)
  }
  .dn-sidebar a:hover .dn-tip{opacity:1;transform:translate(0,-50%)}
  .dn-sidebar-divider{height:1px;margin:4px 6px;background:rgba(255,255,255,.08)}
}
@keyframes dnFade{from{opacity:0}to{opacity:1}}
@keyframes dnSheetUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition:none!important;scroll-behavior:auto!important}}
@keyframes dnPageIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes dnSlide{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
@keyframes dnShine{0%,55%{transform:translateX(-20%) rotate(20deg)}75%,100%{transform:translateX(120%) rotate(20deg)}}
@keyframes dnPulse{0%,100%{opacity:.45;transform:scale(.9)}50%{opacity:1;transform:scale(1.08)}}
"""


DEX_FAVICON_URL = "https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"
DEX_FAVICON_HTML = (
    f'<link rel="icon" type="image/webp" href="{DEX_FAVICON_URL}">'
    f'<link rel="shortcut icon" type="image/webp" href="{DEX_FAVICON_URL}">'
    f'<link rel="apple-touch-icon" href="{DEX_FAVICON_URL}">'
)

GLOBAL_UI_JS = r"""
<script>
(() => {
  document.documentElement.classList.add('dn-ready');
  const path = location.pathname;
  const move = (e) => {
    document.documentElement.style.setProperty('--dn-mx', e.clientX + 'px');
    document.documentElement.style.setProperty('--dn-my', e.clientY + 'px');
  };
  if (window.matchMedia('(pointer:fine)').matches) window.addEventListener('pointermove', move, {passive:true});

  const existingChrome = document.querySelector('.dn-chrome');
  const shouldAddChrome = !existingChrome && path !== '/' && path !== '/obfuscate' && !document.querySelector('.dn-nav');
  if (shouldAddChrome && document.body) {
    const links = [
      ['/obfuscate','Obfustucate'],
      ['/scripts','Scripts'],
      ['/home','Dashboard'],
      ['/chat','Chat'],
      ['/ME-chat','ME-Chat'],
      ['/admin','Admin']
    ];
    const nav = document.createElement('header');
    nav.className = 'dn-chrome';
    const active = (href) => path === href || (href !== '/' && path.startsWith(href + '/'));
    nav.innerHTML = `
      <a class="dn-chrome-brand" href="/">
        <span class="dn-chrome-logo">D</span><span>DexNotifier</span>
      </a>
      <nav class="dn-chrome-links">
        ${links.map(([href,label]) => `<a href="${href}" class="${active(href)?'active':''}">${label}</a>`).join('')}
      </nav>
      <span class="dn-chrome-status"><i></i> Online</span>`;
    document.body.prepend(nav);
  }

  document.querySelectorAll('button,a,.card,.panel,.script-card,.dn-mini').forEach((el) => {
    el.addEventListener('pointerenter', () => el.style.setProperty('will-change','transform'));
    el.addEventListener('pointerleave', () => el.style.removeProperty('will-change'));
  });

  const ICONS = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v11h14V10"/><path d="M9 21v-6h6v6"/>',
    scripts: '<path d="M8 6 3 12l5 6"/><path d="M16 6l5 6-5 6"/>',
    obf: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8 12h8M12 8v8"/>',
    chat: '<path d="M4 5h16v11H9l-4 4V5z"/>',
    panel: '<rect x="3" y="4" width="7" height="7" rx="1.5"/><rect x="14" y="4" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    mechat: '<path d="M4 5h16v11H9l-4 4V5z"/><circle cx="9" cy="10" r="1.1" fill="currentColor" stroke="none"/><circle cx="12.5" cy="10" r="1.1" fill="currentColor" stroke="none"/><circle cx="16" cy="10" r="1.1" fill="currentColor" stroke="none"/>',
    admin: '<path d="M12 3l7 3.5v5.2c0 5-3 8-7 9.3-4-1.3-7-4.3-7-9.3V6.5L12 3z"/>',
    more: '<circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none"/>'
  };
  const svgIcon = (paths) => `<svg viewBox="0 0 24 24">${paths}</svg>`;
  const isActive = (href) => href === '/' ? path === '/' : (path === href || path.startsWith(href + '/'));

  const ALL_LINKS = [
    ['/', 'Home', 'home'],
    ['/scripts', 'Scripts', 'scripts'],
    ['/obfuscate', 'Obf.', 'obf'],
    ['/chat', 'Chat', 'chat'],
    ['/home', 'Panel', 'panel'],
    ['/ME-chat', 'ME-Chat', 'mechat'],
    ['/admin', 'Admin', 'admin'],
  ];
  const TAB_PRIMARY = ['/', '/scripts', '/chat'];
  const skipNav = path === '/login' || path.startsWith('/auth/discord');

  if (!skipNav && document.body && !document.querySelector('.dn-sidebar')) {
    const rail = document.createElement('nav');
    rail.className = 'dn-sidebar';
    rail.innerHTML = ALL_LINKS.map(([href, label, icon]) =>
      `<a href="${href}" class="${isActive(href) ? 'active' : ''}">${svgIcon(ICONS[icon])}<span class="dn-tip">${label}</span></a>`
    ).join('');
    document.body.appendChild(rail);

    const moreLinks = ALL_LINKS.filter(([href]) => !TAB_PRIMARY.includes(href));
    const tabbar = document.createElement('nav');
    tabbar.className = 'dn-tabbar';
    tabbar.innerHTML =
      ALL_LINKS.filter(([href]) => TAB_PRIMARY.includes(href)).map(([href, label, icon]) =>
        `<a href="${href}" class="${isActive(href) ? 'active' : ''}">${svgIcon(ICONS[icon])}<span>${label}</span></a>`
      ).join('') +
      `<button type="button" id="dn-more-btn" class="${moreLinks.some(([h]) => isActive(h)) ? 'active' : ''}">${svgIcon(ICONS.more)}<span>More</span></button>`;
    document.body.appendChild(tabbar);
    document.body.classList.add('dn-has-tabbar');

    const sheet = document.createElement('div');
    sheet.className = 'dn-more-sheet';
    sheet.innerHTML =
      '<div class="dn-more-backdrop"></div>' +
      '<div class="dn-more-panel"><div class="dn-more-grab"></div><div class="dn-more-grid">' +
      moreLinks.map(([href, label, icon]) =>
        `<a href="${href}" class="${isActive(href) ? 'active' : ''}">${svgIcon(ICONS[icon])}<span>${label}</span></a>`
      ).join('') +
      '</div></div>';
    document.body.appendChild(sheet);
    const closeSheet = () => sheet.classList.remove('open');
    document.getElementById('dn-more-btn').addEventListener('click', () => sheet.classList.add('open'));
    sheet.querySelector('.dn-more-backdrop').addEventListener('click', closeSheet);
  }
})();
</script>
"""


@app.middleware("http")
async def dexnotifier_ui_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        original_body = None
        try:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
            original_body = b"".join(chunks)
            body = original_body

            try:
                text = original_body.decode("utf-8", errors="replace")

                if "</head>" in text:
                    if DEX_FAVICON_URL not in text and "rel=\"icon\"" not in text.lower() and "rel='icon'" not in text.lower():
                        text = text.replace("</head>", DEX_FAVICON_HTML + "</head>", 1)
                    if "--dn-bg:" not in text:
                        text = text.replace("</head>", "<style>" + GLOBAL_UI_CSS + "</style></head>", 1)

                if "</body>" in text and "document.documentElement.classList.add('dn-ready')" not in text:
                    text = text.replace("</body>", GLOBAL_UI_JS + "</body>", 1)

                body = text.encode("utf-8")
            except Exception as transform_exc:
                print(f"[UI] middleware transform error (serving untouched body): {transform_exc}")
                body = original_body

            async def _single_body(b=body):
                yield b
            response.body_iterator = _single_body()
            response.headers["content-length"] = str(len(body))
        except Exception as exc:
            print(f"[UI] middleware error: {exc}")
            if original_body is not None:
                async def _fallback_body(b=original_body):
                    yield b
                response.body_iterator = _fallback_body()
                response.headers["content-length"] = str(len(original_body))
    return response

# ═════════════════════════════════════════════════════════════════════════════
# RANDOMNESS
# ═════════════════════════════════════════════════════════════════════════════

_RNG = random.SystemRandom()

_IDENTIFIER_CHARS = string.ascii_letters

_U8 = 0x100
_U16 = 0x10000

_PRNG_MULT = 25173
_PRNG_ADD = 13849

_MIX_A = 197
_MIX_B = 113
_MIX_C = 71
_MIX_D = 43


# ═════════════════════════════════════════════════════════════════════════════
# IDENTIFIERS
# ═════════════════════════════════════════════════════════════════════════════

def _rand_name(length=None):
    if length is None:
        length = _RNG.randint(9, 17)

    length = max(2, int(length))

    confusing = (
        string.ascii_letters
        + "IlIOoO"
        + "lI1"
        + "oO0"
    )

    result = _RNG.choice(string.ascii_letters)

    for _ in range(length - 1):
        result += _RNG.choice(confusing)

    return result


def _unique_name(used):
    while True:
        name = _rand_name()

        if name not in used:
            used.add(name)
            return name


# ═════════════════════════════════════════════════════════════════════════════
# INTEGER HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _u8(value):
    return int(value) & 0xFF


def _u16(value):
    return int(value) & 0xFFFF


def _prng_step(state, extra=0):
    return (
        int(state) * _PRNG_MULT
        + _PRNG_ADD
        + int(extra)
    ) % _U16


# ═════════════════════════════════════════════════════════════════════════════
# ROTATION
# ═════════════════════════════════════════════════════════════════════════════

def _rotl8(value, amount):
    value = _u8(value)
    amount = int(amount) & 7

    if amount == 0:
        return value

    return (
        ((value << amount) & 0xFF)
        | (value >> (8 - amount))
    )


def _rotr8(value, amount):
    value = _u8(value)
    amount = int(amount) & 7

    if amount == 0:
        return value

    return (
        (value >> amount)
        | ((value << (8 - amount)) & 0xFF)
    )


# ═════════════════════════════════════════════════════════════════════════════
# PERMUTATION
# ═════════════════════════════════════════════════════════════════════════════

def _make_permutation(length, seed):
    length = int(length)
    state = _u16(seed)

    permutation = list(range(length))

    for position in range(length - 1, 0, -1):
        state = _prng_step(
            state,
            position * 97
        )

        swap_index = state % (position + 1)

        permutation[position], permutation[swap_index] = (
            permutation[swap_index],
            permutation[position],
        )

    return permutation


# ═════════════════════════════════════════════════════════════════════════════
# ONE CIPHER ROUND
# ═════════════════════════════════════════════════════════════════════════════

def _encrypt_round(
    source,
    seed1,
    seed2,
    seed3,
    seed4,
    block_size
):
    encrypted = bytearray()

    for block_start in range(
        0,
        len(source),
        block_size
    ):
        block = source[
            block_start:
            block_start + block_size
        ]

        block_length = len(block)

        permutation_seed = (
            seed1
            + seed2
            + seed3 * (block_start + 1)
            + seed4 * block_length
            + block_start * _MIX_A
        ) % _U16

        permutation = _make_permutation(
            block_length,
            permutation_seed
        )

        state = (
            seed1
            + seed3
            + ((block_start + 1) * 17)
            + (block_length * _MIX_B)
        ) % _U16

        previous_cipher = (
            seed4
            + block_start
            + block_length
        ) & 0xFF

        transformed = [0] * block_length

        for destination in range(block_length):
            original_index = permutation[destination]

            absolute_index = (
                block_start
                + original_index
                + 1
            )

            state = _prng_step(
                state,
                original_index
                + destination
                + block_length
            )

            value = block[original_index]

            rotation = (
                seed2
                + original_index
                + destination
                + state
                + block_length
            ) & 7

            value = _rotl8(
                value,
                rotation
            )

            add_value = (
                (state >> 8)
                ^ (seed4 & 0xFF)
                ^ (absolute_index * 13)
                ^ (destination * _MIX_C)
            ) & 0xFF

            value = (
                value + add_value
            ) & 0xFF

            xor_value = (
                (seed3 & 0xFF)
                + destination * 29
                + (state & 0xFF)
                + absolute_index * 7
                + block_length * _MIX_D
            ) & 0xFF

            value ^= xor_value

            feedback = (
                previous_cipher
                ^ ((state >> 8) & 0xFF)
                ^ (seed1 & 0xFF)
                ^ ((absolute_index * 11) & 0xFF)
            ) & 0xFF

            value ^= feedback

            final_mix = (
                (seed4 >> 8)
                + destination * 17
                + original_index * 31
                + state
            ) & 0xFF

            value ^= final_mix
            value &= 0xFF

            previous_cipher = value
            transformed[destination] = value

        encrypted.extend(transformed)

    return bytes(encrypted)


def _decrypt_round(
    encrypted,
    seed1,
    seed2,
    seed3,
    seed4,
    block_size
):
    decrypted = bytearray()

    for block_start in range(
        0,
        len(encrypted),
        block_size
    ):
        block = encrypted[
            block_start:
            block_start + block_size
        ]

        block_length = len(block)

        permutation_seed = (
            seed1
            + seed2
            + seed3 * (block_start + 1)
            + seed4 * block_length
            + block_start * _MIX_A
        ) % _U16

        permutation = _make_permutation(
            block_length,
            permutation_seed
        )

        state = (
            seed1
            + seed3
            + ((block_start + 1) * 17)
            + (block_length * _MIX_B)
        ) % _U16

        previous_cipher = (
            seed4
            + block_start
            + block_length
        ) & 0xFF

        output = [0] * block_length

        for destination in range(block_length):
            original_index = permutation[destination]

            absolute_index = (
                block_start
                + original_index
                + 1
            )

            state = _prng_step(
                state,
                original_index
                + destination
                + block_length
            )

            value = block[destination]

            current_cipher = value

            final_mix = (
                (seed4 >> 8)
                + destination * 17
                + original_index * 31
                + state
            ) & 0xFF

            value ^= final_mix

            feedback = (
                previous_cipher
                ^ ((state >> 8) & 0xFF)
                ^ (seed1 & 0xFF)
                ^ ((absolute_index * 11) & 0xFF)
            ) & 0xFF

            value ^= feedback

            xor_value = (
                (seed3 & 0xFF)
                + destination * 29
                + (state & 0xFF)
                + absolute_index * 7
                + block_length * _MIX_D
            ) & 0xFF

            value ^= xor_value

            add_value = (
                (state >> 8)
                ^ (seed4 & 0xFF)
                ^ (absolute_index * 13)
                ^ (destination * _MIX_C)
            ) & 0xFF

            value = (
                value - add_value
            ) & 0xFF

            rotation = (
                seed2
                + original_index
                + destination
                + state
                + block_length
            ) & 7

            value = _rotr8(
                value,
                rotation
            )

            output[original_index] = value

            previous_cipher = current_cipher

        decrypted.extend(output)

    return bytes(decrypted)


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRITY
# ═════════════════════════════════════════════════════════════════════════════

def _integrity_digest(data):
    h1 = 0x1357
    h2 = 0x2468
    h3 = 0x369C
    h4 = 0x4ACE

    for index, value in enumerate(data, 1):
        value = int(value)

        h1 = (
            h1 * 257
            + value
            + index
        ) % _U16

        h2 = (
            h2 * 263
            + value * 3
            + index * 7
        ) % _U16

        h3 = (
            h3 * 269
            + value * 5
            + index * 11
        ) % _U16

        h4 = (
            h4 * 271
            + value * 7
            + index * 17
        ) % _U16

    return h1, h2, h3, h4


def _cipher_digest(data):
    h1 = 0x5A31
    h2 = 0x71C9
    h3 = 0x42D7

    for index, value in enumerate(data, 1):
        value = int(value)

        h1 = (
            h1 * 251
            + value
            + index * 3
        ) % _U16

        h2 = (
            h2 * 277
            + value * 7
            + index * 13
        ) % _U16

        h3 = (
            h3 * 283
            + value * 11
            + index * 19
        ) % _U16

    return h1, h2, h3


# ═════════════════════════════════════════════════════════════════════════════
# OPAQUE NUMBERS
# ═════════════════════════════════════════════════════════════════════════════

def _num_expr(value):
    value = int(value)

    if value == 0:
        a = _RNG.randint(100, 9000)
        return f"({a}-{a})"

    if value < 0:
        return f"-({_num_expr(-value)})"

    style = _RNG.randint(0, 8)

    if style == 0:
        a = _RNG.randint(1, 5000)
        return f"({a}+({value-a}))"

    if style == 1:
        a = _RNG.randint(value + 1, value + 5000)
        return f"({a}-{a-value})"

    if style == 2:
        a = _RNG.randint(1, 1000)
        return f"(({value+a})-{a})"

    if style == 3:
        a = _RNG.randint(1, 200)
        b = _RNG.randint(1, 200)
        return f"(({value+a})+{b}-{a}-{b})"

    if style == 4:
        a = _RNG.randint(1, 100)
        return f"(({value}*1)+{a}-{a})"

    if style == 5:
        a = _RNG.randint(2, 31)
        return f"(({value}*{a})/{a})"

    if style == 6:
        a = _RNG.randint(1, 300)
        b = _RNG.randint(1, 300)
        return f"((({value}+{a})-{a})+({b}-{b}))"

    if style == 7:
        a = _RNG.randint(1, 1000)
        return f"(({value}+{a})-{a})"

    a = _RNG.randint(2, 19)
    return f"(({value}*{a})/{a})"


# ═════════════════════════════════════════════════════════════════════════════
# LUA ESCAPING
# ═════════════════════════════════════════════════════════════════════════════

def _lua_escape_bytes(data):
    return "".join(
        f"\\{int(value):03d}"
        for value in data
    )


# ═════════════════════════════════════════════════════════════════════════════
# RANDOM TOKEN ALPHABET
# ═════════════════════════════════════════════════════════════════════════════

def _make_token_alphabet():
    candidates = list(
        "!#$%&()*+,-./:;<=>?@[]^_{|}~"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
    )

    token_zero = _RNG.choice(candidates)

    remaining = [
        character
        for character in candidates
        if character != token_zero
    ]

    token_one = _RNG.choice(remaining)

    return token_zero, token_one


def _encode_binary_tokens(
    data,
    token_zero,
    token_one
):
    output = []

    for value in data:
        value = _u8(value)

        for shift in range(7, -1, -1):
            if value & (1 << shift):
                output.append(token_one)
            else:
                output.append(token_zero)

    return "".join(output)


def _decode_binary_tokens(
    tokens,
    token_zero,
    token_one
):
    if not isinstance(tokens, str):
        raise TypeError(
            "Token payload must be a string."
        )

    if len(tokens) % 8 != 0:
        raise ValueError(
            "Token payload is not byte aligned."
        )

    output = bytearray()

    for cursor in range(
        0,
        len(tokens),
        8
    ):
        value = 0

        for character in tokens[
            cursor:
            cursor + 8
        ]:
            value <<= 1

            if character == token_one:
                value |= 1

            elif character != token_zero:
                raise ValueError(
                    "Invalid token character."
                )

        output.append(value)

    return bytes(output)


# ═════════════════════════════════════════════════════════════════════════════
# RANDOM FRAGMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def _fragment_tokens(encoded, min_chunks=13, max_chunks=113):
    fragments = []
    cursor = 0

    while cursor < len(encoded):
        amount = (
            _RNG.randint(min_chunks, max_chunks)
            * 8
        )

        fragments.append(
            encoded[
                cursor:
                cursor + amount
            ]
        )

        cursor += amount

    indexed = list(
        enumerate(fragments)
    )

    _RNG.shuffle(indexed)

    return indexed


# ═════════════════════════════════════════════════════════════════════════════
# RUNTIME NOISE
# ═════════════════════════════════════════════════════════════════════════════

def _make_noise_expression():
    a = _RNG.randint(1000, 9000)
    b = _RNG.randint(1000, 9000)

    return (
        f"(({a}*{b})-"
        f"({a}*{b}))"
    )


def _resolve_data_dir() -> str:
    candidate = (os.environ.get("DEX_DATA_DIR", "/data").strip() or "/data")
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    def _write_test(path: str) -> None:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".dn_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)

    try:
        _write_test(candidate)
        return candidate
    except PermissionError as e:
        try:
            os.chmod(candidate, 0o777)
            _write_test(candidate)
            return candidate
        except Exception:
            os.makedirs(fallback, exist_ok=True)
            print(f"[DATA_DIR] PERMISSION DENIED writing to '{candidate}' ({e}). Falling back to '{fallback}'.")
            return fallback
    except Exception as e:
        os.makedirs(fallback, exist_ok=True)
        print(f"[DATA_DIR] Could not use '{candidate}' ({e}). Falling back to '{fallback}'.")
        return fallback


def _write_test(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".dn_write_test")
    with open(probe, "w") as f:
        f.write("ok")
    os.remove(probe)


DATA_DIR = _resolve_data_dir()
_USING_FALLBACK_DATA_DIR = DATA_DIR != (os.environ.get("DEX_DATA_DIR", "/data").strip() or "/data")
if _USING_FALLBACK_DATA_DIR:
    print(f"[DATA_DIR] *** WARNING: running WITHOUT persistent storage. Using ephemeral '{DATA_DIR}' ***")
else:
    try:
        import shutil as _shutil
        _total, _used, _free = _shutil.disk_usage(DATA_DIR)
        print(f"[DATA_DIR] Persistent data directory in use: {DATA_DIR} ({_free // (1024**2)} MB free of {_total // (1024**2)} MB)")
    except Exception:
        print(f"[DATA_DIR] Persistent data directory in use: {DATA_DIR}")


def _get_or_create_secret(env_name: str, file_name: str) -> str:
    val = os.environ.get(env_name)
    if val:
        return val.strip()
    if os.path.exists(file_name):
        with open(file_name, "r", encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing
    generated = secrets.token_urlsafe(32)
    try:
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(generated)
        os.chmod(file_name, 0o600)
    except Exception as e:
        print(f"WARNING: could not persist generated secret for {env_name}: {e}")
    print(f"[SECURITY] {env_name} was not set. Generated a new one and saved it to {file_name}.")
    return generated


API_KEY = (os.environ.get("DEX_API_KEY", "").strip() or "")
ADMIN_PASSWORD = os.environ.get("DEX_ADMIN_KEY", "").strip()
SECRET_KEY = _get_or_create_secret("DEX_SECRET_KEY", os.path.join(DATA_DIR, ".dex_secret_key"))
BASE_URL = os.environ.get("DEX_BASE_URL", "https://dexapi1.up.railway.app").rstrip("/")

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "").strip()
DISCORD_OAUTH_SCOPE = "identify"
DISCORD_OWNER_USERNAME = os.environ.get("DEX_OWNER_DISCORD_USERNAME", "lyubomyr2012_official").strip().lower()


def discord_oauth_configured() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)


DISCORD_ERROR_MESSAGES = {
    "access_denied": "Discord sign-in was cancelled.",
    "invalid_state": "Your sign-in attempt expired or couldn't be verified - click Continue with Discord to try again.",
    "token_exchange_failed": "Discord sign-in failed while contacting Discord - please try again.",
    "profile_fetch_failed": "Signed in with Discord but couldn't load your profile - please try again.",
}


def discord_error_message(code: str) -> str:
    if not code:
        return ""
    return DISCORD_ERROR_MESSAGES.get(code, "Discord sign-in failed - please try again.")


def constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_valid_key(k: str) -> bool:
    return bool(k) and constant_time_eq(k, API_KEY)


FIXED_RAW_SCRIPT_URLS: Dict[str, str] = {
    "dexfree": "https://raw.githubusercontent.com/lyubomyrivanytskyy24-ops/DexFreeWSS/refs/heads/main/scripts/dexfree.lua",
    "dexserverhop": "https://raw.githubusercontent.com/lyubomyrivanytskyy24-ops/DexFreeWSS/refs/heads/main/scripts/dexserverhop.lua",
    "dexcodesniper": "https://raw.githubusercontent.com/lyubomyrivanytskyy24-ops/DexFreeWSS/refs/heads/main/scripts/dexcodesniper.lua",
}

GITHUB_OWNER = os.environ.get("DEX_GITHUB_OWNER", "").strip()
GITHUB_REPO = os.environ.get("DEX_GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.environ.get("DEX_GITHUB_BRANCH", "main").strip() or "main"
GITHUB_TOKEN = os.environ.get("DEX_GITHUB_TOKEN", "").strip()
GITHUB_CACHE_TTL = max(60, int(os.environ.get("DEX_GITHUB_CACHE_TTL", "60")))

GITHUB_SCRIPT_PATHS: Dict[str, str] = {
    "dexchilli": os.environ.get("DEX_GITHUB_PATH_DEXCHILLI", "scripts/dexchilli.lua").strip(),
    "dexfree": os.environ.get("DEX_GITHUB_PATH_DEXFREE", "scripts/dexfree.lua").strip(),
    "dexserverhop": os.environ.get("DEX_GITHUB_PATH_DEXSERVERHOP", "scripts/dexserverhop.lua").strip(),
    "dexhub": os.environ.get("DEX_GITHUB_PATH_DEXHUB", "scripts/dexhub.lua").strip(),
    "dexpaid": os.environ.get("DEX_GITHUB_PATH_DEXPAID", "scripts/dexpaid.lua").strip(),
    "dexautoroll": os.environ.get("DEX_GITHUB_PATH_DEXAUTOROLL", "scripts/dexautoroll.lua").strip(),
    "dexcodesniper": os.environ.get("DEX_GITHUB_PATH_DEXCODESNIPER", "scripts/dexcodesniper.lua").strip(),
}

_github_cache: Dict[str, Dict[str, Any]] = {}
_github_cache_lock = asyncio.Lock()
_github_last_status: Dict[str, Dict[str, Any]] = {}
_github_status_lock = asyncio.Lock()
GITHUB_USER_AGENT = "DexNotifier-API/1.2 (+direct-plain-text-script-sync)"


def github_configured() -> bool:
    return bool(GITHUB_OWNER and GITHUB_REPO)


def github_repo_url() -> str:
    if not github_configured():
        return ""
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/blob/{GITHUB_BRANCH}"


def _github_raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{path.lstrip('/')}"


def _github_api_contents_url(path: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path.lstrip('/')}?ref={GITHUB_BRANCH}"


def _decode_plain_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _fetch_plain_text_url_sync(url: str) -> "tuple[Optional[str], str]":
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", GITHUB_USER_AGENT)
    req.add_header("Accept", "text/plain, text/*;q=0.9, */*;q=0.1")
    req.add_header("Cache-Control", "no-cache")
    req.add_header("Pragma", "no-cache")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = getattr(resp, "status", resp.getcode())
            if status != 200:
                raise urllib.error.HTTPError(url, status, "unexpected status", resp.headers, None)
            return _decode_plain_text(resp.read()), ""
    except Exception as exc:
        return None, f"plain-text fetch failed ({exc})"


def _fetch_github_raw_sync(path: str) -> "tuple[Optional[str], str]":
    if not github_configured():
        return None, "GitHub repo not configured (DEX_GITHUB_OWNER / DEX_GITHUB_REPO unset)"
    return _fetch_plain_text_url_sync(_github_raw_url(path))


def _fetch_script_sync(name: str) -> "tuple[Optional[str], str]":
    fixed_url = FIXED_RAW_SCRIPT_URLS.get(name)
    if fixed_url:
        return _fetch_plain_text_url_sync(fixed_url)
    path = GITHUB_SCRIPT_PATHS.get(name, "")
    if not path:
        return None, "no source path configured"
    return _fetch_github_raw_sync(path)


def _script_source_url(name: str) -> str:
    return FIXED_RAW_SCRIPT_URLS.get(name) or (
        _github_raw_url(GITHUB_SCRIPT_PATHS[name])
        if github_configured() and GITHUB_SCRIPT_PATHS.get(name) else ""
    )


async def _record_github_status(name: str, ok: bool, error: str = "") -> None:
    async with _github_status_lock:
        _github_last_status[name] = {"ok": ok, "checked_at": time.time(), "error": error}


def get_github_status(name: str) -> Optional[Dict[str, Any]]:
    return _github_last_status.get(name)


async def get_github_script(name: str, local_fallback_file: str, default: str) -> str:
    now = time.time()
    async with _github_cache_lock:
        cached = _github_cache.get(name)
        if cached and (now - cached["fetched_at"]) < GITHUB_CACHE_TTL:
            return cached["content"]

    content, err = await asyncio.to_thread(_fetch_script_sync, name)
    if content is not None:
        source = "raw_url" if name in FIXED_RAW_SCRIPT_URLS else "github"
        async with _github_cache_lock:
            _github_cache[name] = {"content": content, "fetched_at": now, "source": source}
        await _record_github_status(name, True)
        try:
            save_file(local_fallback_file, content)
        except Exception as exc:
            print(f"[SCRIPTS] local mirror write failed for {name}: {exc}")
        return content

    await _record_github_status(name, False, err or "unknown fetch error")
    async with _github_cache_lock:
        cached = _github_cache.get(name)
        if cached and cached.get("content") is not None:
            return cached["content"]

    fallback = load_file(local_fallback_file, default)
    async with _github_cache_lock:
        _github_cache[name] = {"content": fallback, "fetched_at": now, "source": "local_fallback"}
    return fallback


async def force_refresh_github_cache():
    async with _github_cache_lock:
        _github_cache.clear()


async def refresh_all_github_scripts() -> Dict[str, Dict[str, Any]]:
    await force_refresh_github_cache()
    results: Dict[str, Dict[str, Any]] = {}
    for name, meta in FIXED_SCRIPTS.items():
        content = await get_github_script(name, meta["file"], meta["default"])
        status = get_github_status(name)
        cache_meta = get_cache_meta(name)
        results[name] = {
            "source": cache_meta.get("source") if cache_meta else "unknown",
            "ok": bool(status.get("ok")) if status else (cache_meta or {}).get("source") == "github",
            "error": status.get("error", "") if status else "",
            "bytes": len(content or ""),
        }
    return results


def get_cache_meta(name: str) -> Optional[Dict[str, Any]]:
    return _github_cache.get(name)


SESSION_MAX_AGE = 7 * 24 * 3600
ADMIN_SESSION_MAX_AGE = 2 * 3600


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(subject: str) -> str:
    ts = str(int(time.time()))
    payload = f"{subject}|{ts}"
    sig = _sign(payload)
    raw = f"{payload}|{sig}"
    return raw.replace("|", ".")


def verify_session_token(token: Optional[str], max_age: int) -> Optional[str]:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    subject, ts, sig = parts
    payload = f"{subject}|{ts}"
    expected_sig = _sign(payload)
    if not constant_time_eq(sig, expected_sig):
        return None
    try:
        ts_int = int(ts)
    except ValueError:
        return None
    if time.time() - ts_int > max_age:
        return None
    return subject


RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 15 * 60
RATE_LIMIT_LOCKOUT = 15 * 60

_rate_state: Dict[str, Dict[str, Any]] = {}
_rate_lock = asyncio.Lock()

_IP_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F:.]{2,45}$")


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        candidate = xff.split(",")[0].strip()
        if _IP_TOKEN_PATTERN.match(candidate):
            return candidate
    if request.client:
        return request.client.host
    return "unknown"


def _ws_client_ip(websocket: WebSocket) -> str:
    xff = websocket.headers.get("x-forwarded-for")
    if xff:
        candidate = xff.split(",")[0].strip()
        if _IP_TOKEN_PATTERN.match(candidate):
            return candidate
    if websocket.client:
        return websocket.client.host
    return "unknown"


async def is_rate_limited(bucket: str, ip: str) -> bool:
    key = f"{bucket}:{ip}"
    async with _rate_lock:
        state = _rate_state.get(key)
        if not state:
            return False
        now = time.time()
        if state.get("locked_until", 0) > now:
            return True
        if now - state.get("window_start", 0) > RATE_LIMIT_WINDOW:
            _rate_state.pop(key, None)
            return False
        return False


async def record_failed_attempt(bucket: str, ip: str):
    key = f"{bucket}:{ip}"
    async with _rate_lock:
        now = time.time()
        state = _rate_state.get(key)
        if not state or now - state.get("window_start", 0) > RATE_LIMIT_WINDOW:
            state = {"count": 0, "window_start": now, "locked_until": 0}
        state["count"] += 1
        if state["count"] >= RATE_LIMIT_MAX_ATTEMPTS:
            state["locked_until"] = now + RATE_LIMIT_LOCKOUT
        _rate_state[key] = state


async def clear_attempts(bucket: str, ip: str):
    key = f"{bucket}:{ip}"
    async with _rate_lock:
        _rate_state.pop(key, None)


_volume_buckets: Dict[str, deque] = defaultdict(deque)


def rate_limited(ip: str, bucket: str, max_requests: int, window_seconds: float) -> bool:
    key = f"{bucket}:{ip}"
    now = time.monotonic()
    q = _volume_buckets[key]
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= max_requests:
        return True
    q.append(now)
    return False


async def rate_bucket_janitor():
    while True:
        await asyncio.sleep(600)
        now = time.monotonic()
        stale_keys = [k for k, q in _volume_buckets.items() if not q or now - q[-1] > 3600]
        for k in stale_keys:
            _volume_buckets.pop(k, None)


def reject_if_oversized(request: Request, max_bytes: int) -> bool:
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return True
        except ValueError:
            return True
    return False


BLOCKED_SUBSTRINGS = {
    "discord", "discordgg", "discordcom", "discordappcom", "dscgg",
    "fuck", "shit", "bitch", "asshole", "cunt", "dick", "cock", "pussy",
    "nigger", "nigga", "faggot", "fag", "retard", "whore", "slut",
    "porn", "rape", "nazi", "kike", "chink", "spic",
}

_LEET_TRANSLATION = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s",
})

_URL_PATTERN = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STRICT_SINGLE_LINE_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_NUL_BYTE_PATTERN = re.compile(r"\x00")
_ANY_WHITESPACE_PATTERN = re.compile(r"\s")

WSS_URL_PATTERN = re.compile(
    r"^wss?://[A-Za-z0-9.\-]{1,253}(:\d{1,5})?(/[A-Za-z0-9._~\-/%]*)?$"
)

USERNAME_FORMAT_PATTERN = re.compile(r"^(?!.*[_.]{2})[A-Za-z0-9][A-Za-z0-9_.]{1,30}[A-Za-z0-9]$")
KEY_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
HWID_PARAM_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def normalize_field(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _normalize_for_matching(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = stripped.lower().translate(_LEET_TRANSLATION)
    return re.sub(r"[^a-z0-9]+", "", stripped)


def contains_blocked_content(*fields: str) -> bool:
    for field in fields:
        if _URL_PATTERN.search(field.lower()):
            return True
        normalized = _normalize_for_matching(field)
        for bad in BLOCKED_SUBSTRINGS:
            if bad in normalized:
                return True
    return False


def has_excessive_repetition(text: str, max_repeat: int = 6) -> bool:
    return bool(re.search(r"(.)\1{" + str(max_repeat) + r",}", text))


USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
SLUG_SOURCE_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,48}$")
RESERVED_USERNAMES = {"system", "admin", "administrator", "root", "sender", "owner", "sys"}

MAX_GENERIC_BODY = 8 * 1024
MAX_SCRIPT_BODY = 16 * 1024 * 1024
MAX_FORM_BODY = 2 * 1024 * 1024 + (100 * 1024)
MAX_PASSWORD_LEN = 128
MAX_LOG_LEN = 4096
MAX_USERNAME_LEN = 32
MAX_BANNER_LEN = 500


def is_valid_username(username: str) -> bool:
    if not USERNAME_RE.match(username):
        return False
    if username.lower() in RESERVED_USERNAMES:
        return False
    if contains_blocked_content(username):
        return False
    return True


def make_slug(name: str) -> Optional[str]:
    if not SLUG_SOURCE_RE.match(name):
        return None
    slug = name.strip().replace(" ", "-")
    if not slug:
        return None
    return slug


PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk, expected)


USERNAME_FILE = os.path.join(DATA_DIR, "usernames.txt")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklisted.txt")
LOGS_FILE = os.path.join(DATA_DIR, "logs.txt")
DEXPAID_KEYS_FILE = os.path.join(DATA_DIR, "dexpaid_keys.json")
BANNER_FILE = os.path.join(DATA_DIR, "banner.txt")
ANNOUNCEMENT_FILE = os.path.join(DATA_DIR, "announcement.txt")

USERS_FILE = os.path.join(DATA_DIR, "users.json")
SCRIPTS_FILE = os.path.join(DATA_DIR, "scripts.json")
CHAT_DATA_DIR = os.environ.get("DEX_CHAT_DATA_DIR", "").strip() or os.path.join(DATA_DIR, "chat_data")
CHAT_MEDIA_DIR = os.path.join(CHAT_DATA_DIR, "media")
CHAT_HISTORY_FILE = os.path.join(CHAT_DATA_DIR, "chat_messages.json")
ME_CHAT_HISTORY_FILE = os.path.join(CHAT_DATA_DIR, "me_chat_messages.json")
ME_GROUP_FILE = os.path.join(CHAT_DATA_DIR, "me_group.json")
CHAT_HISTORY_MAX = 3000
CHAT_MAX_MESSAGE = 4000
CHAT_MAX_JSON = 12 * 1024 * 1024
CHAT_MAX_MEDIA_BYTES = 8 * 1024 * 1024
CHAT_RATE_LIMIT = 1
CHAT_RATE_WINDOW = 2.0
CHAT_ALLOWED_MEDIA = {"image/jpeg":".jpg","image/png":".png","image/gif":".gif","image/webp":".webp","video/mp4":".mp4","video/webm":".webm","video/quicktime":".mov"}
chat_lock=asyncio.Lock(); me_chat_lock=asyncio.Lock(); me_group_lock=asyncio.Lock()
chat_connections:Set[WebSocket]=set(); me_chat_connections:Set[WebSocket]=set()
os.makedirs(CHAT_DATA_DIR,exist_ok=True); os.makedirs(CHAT_MEDIA_DIR,exist_ok=True)

lock = asyncio.Lock()
blacklist_lock = asyncio.Lock()
announcement_lock = asyncio.Lock()
logs_lock = asyncio.Lock()
dexpaid_keys_lock = asyncio.Lock()
users_lock = asyncio.Lock()
scripts_lock = asyncio.Lock()
ws_count_lock = asyncio.Lock()
banner_lock = asyncio.Lock()

DEXCHILLI_FILE = "dexchilli.lua"
DEXFREE_FILE = "dexfree.lua"
DEXSERVERHOP_FILE = "dexserverhop.lua"
DEXHUB_FILE = "dexhub.lua"
DEXPAID_FILE = "dexpaid.lua"
DEXAUTOROLL_FILE = "dexautoroll.lua"
DEXCODESNIPER_FILE = "dexcodesniper.lua"

DEFAULT_DEXCHILLI = "-- DexChilli loader script not set yet. Add scripts/dexchilli.lua to the GitHub repo."
DEFAULT_DEXFREE = "-- DexFree loader script not set yet. Add scripts/dexfree.lua to the GitHub repo."
DEFAULT_DEXSERVERHOP = "-- DexServerHop loader script not set yet. Add scripts/dexserverhop.lua to the GitHub repo."
DEFAULT_DEXHUB = "-- DexHub loader script not set yet. Add scripts/dexhub.lua to the GitHub repo."
DEFAULT_DEXPAID = "-- DexPaid loader script not set yet. Add scripts/dexpaid.lua to the GitHub repo."
DEFAULT_DEXAUTOROLL = "-- DexAutoRoll loader script not set yet. Add scripts/dexautoroll.lua to the GitHub repo."
DEFAULT_DEXCODESNIPER = "-- DexCodeSniper loader script not set yet. Add scripts/dexcodesniper.lua to the GitHub repo."

FIXED_SCRIPTS: Dict[str, Dict[str, str]] = {
    "dexchilli": {"file": DEXCHILLI_FILE, "default": DEFAULT_DEXCHILLI, "label": "DexChilli"},
    "dexfree": {"file": DEXFREE_FILE, "default": DEFAULT_DEXFREE, "label": "DexFree"},
    "dexserverhop": {"file": DEXSERVERHOP_FILE, "default": DEFAULT_DEXSERVERHOP, "label": "DexServerHop"},
    "dexhub": {"file": DEXHUB_FILE, "default": DEFAULT_DEXHUB, "label": "DexHub"},
    "dexpaid": {"file": DEXPAID_FILE, "default": DEFAULT_DEXPAID, "label": "DexPaid"},
    "dexautoroll": {"file": DEXAUTOROLL_FILE, "default": DEFAULT_DEXAUTOROLL, "label": "DexAutoRoll"},
    "dexcodesniper": {"file": DEXCODESNIPER_FILE, "default": DEFAULT_DEXCODESNIPER, "label": "DexCodeSniper"},
}

SCRIPT_TAGLINES: Dict[str, str] = {
    "dexchilli": "Smooth, reliable, and free to run.",
    "dexfree": "The classic free loader - no key required.",
    "dexserverhop": "Automatic server hopping on demand.",
    "dexhub": "The full hub experience, free tier.",
    "dexautoroll": "Set it and forget it automation.",
    "dexcodesniper": "GitHub-managed DexCodeSniper loader.",
}

viewers: Set[WebSocket] = set()
sender_ws: Optional[WebSocket] = None
ws_ip_connection_counts: Dict[str, int] = defaultdict(int)

def load_announcement_from_file() -> str:
    if not os.path.exists(ANNOUNCEMENT_FILE):
        return ""
    try:
        with open(ANNOUNCEMENT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


announcement_text: str = load_announcement_from_file()
announcement_timestamp: float = time.time() if announcement_text else 0.0

dexpaid_keys: Dict[str, float] = {}
last_generated_paid_key: str = ""
last_generated_paid_loadstring: str = ""

users: Dict[str, Dict[str, Any]] = {}
scripts: Dict[str, Dict[str, Any]] = {}


def _atomic_write(path: str, content: str, mode: int = 0o600):
    tmp_path = f"{path}.tmp.{secrets.token_hex(4)}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


MAX_STORED_USERNAMES = 5000


def load_usernames_from_file() -> set:
    if not os.path.exists(USERNAME_FILE):
        return set()
    with open(USERNAME_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_usernames_to_file(names: set):
    _atomic_write(USERNAME_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


stored_usernames: set = load_usernames_from_file()
stored_usernames_lower: set = {u.lower() for u in stored_usernames}


def _purge_bad_usernames_locked() -> bool:
    bad = set()
    for name in stored_usernames:
        if not name:
            bad.add(name)
            continue
        if _ANY_WHITESPACE_PATTERN.search(name):
            bad.add(name)
            continue
        if not is_valid_username(name):
            bad.add(name)
            continue
    if bad:
        for name in bad:
            stored_usernames.discard(name)
            stored_usernames_lower.discard(name.lower())
        return True
    return False


async def username_cleanup_janitor():
    while True:
        await asyncio.sleep(300)
        async with lock:
            changed = _purge_bad_usernames_locked()
            if changed:
                save_usernames_to_file(stored_usernames)


def restart_process():
    print("[USERNAMES] Reached MAX_STORED_USERNAMES - restarting process.")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def schedule_restart(delay: float = 1.0):
    await asyncio.sleep(delay)
    restart_process()


def load_blacklist_from_file() -> set:
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f.readlines() if line.strip())


def save_blacklist_to_file(names: set):
    _atomic_write(BLACKLIST_FILE, "\n".join(sorted(names)) + ("\n" if names else ""), mode=0o644)


blacklisted_usernames: set = load_blacklist_from_file()


def load_banner_from_file() -> str:
    if not os.path.exists(BANNER_FILE):
        return ""
    with open(BANNER_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_banner_to_file(text: str):
    _atomic_write(BANNER_FILE, text, mode=0o644)


def save_announcement_to_file(text: str):
    _atomic_write(ANNOUNCEMENT_FILE, text, mode=0o644)

banner_text: str = load_banner_from_file()

CHAT_SETTINGS_FILE = os.path.join(CHAT_DATA_DIR, "chat_settings.json")
chat_settings_lock = asyncio.Lock()

def _load_chat_settings() -> dict:
    defaults = {"chat_enabled": True, "me_chat_enabled": True}
    if not os.path.exists(CHAT_SETTINGS_FILE):
        return defaults
    try:
        with open(CHAT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in defaults:
            if k in saved:
                defaults[k] = bool(saved[k])
    except Exception:
        pass
    return defaults

def _save_chat_settings(settings: dict) -> None:
    _atomic_write(CHAT_SETTINGS_FILE, json.dumps(settings), mode=0o644)

_chat_settings_initial = _load_chat_settings()
chat_enabled: bool = _chat_settings_initial["chat_enabled"]
me_chat_enabled: bool = _chat_settings_initial["me_chat_enabled"]


def load_file(path: str, default: str) -> str:
    if not os.path.exists(path):
        _atomic_write(path, default, mode=0o644)
        return default
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_file(path: str, content: str):
    _atomic_write(path, content, mode=0o644)


MAX_STORED_LOGS = 5000


def load_logs_from_file() -> list:
    if not os.path.exists(LOGS_FILE):
        return []
    with open(LOGS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f.readlines() if line.strip()][-MAX_STORED_LOGS:]


def append_log_to_file(entry: str):
    with open(LOGS_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


stored_logs: list = load_logs_from_file()


def load_dexpaid_keys_from_file() -> Dict[str, float]:
    if not os.path.exists(DEXPAID_KEYS_FILE):
        return {}
    try:
        with open(DEXPAID_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {k: float(v) for k, v in data.items()}
    except Exception:
        return {}


def save_dexpaid_keys_to_file(keys: Dict[str, float]):
    _atomic_write(DEXPAID_KEYS_FILE, json.dumps(keys), mode=0o600)


dexpaid_keys = load_dexpaid_keys_from_file()


def generate_paid_key(length: int = 24) -> str:
    return secrets.token_urlsafe(length)[:length]


def cleanup_expired_paid_keys():
    now = time.time()
    expired = [k for k, exp in dexpaid_keys.items() if exp <= now]
    for k in expired:
        dexpaid_keys.pop(k, None)


MAX_KEY_DURATION_HOURS = 24 * 365


def parse_duration_hours(raw: str) -> Optional[float]:
    try:
        hours = float(raw)
    except Exception:
        return None
    if hours <= 0 or hours > MAX_KEY_DURATION_HOURS:
        return None
    return hours


def load_users_from_file() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_users_to_file():
    _atomic_write(USERS_FILE, json.dumps(users), mode=0o600)

def _load_me_group_file() -> Set[str]:
    if not os.path.exists(ME_GROUP_FILE): return set()
    try:
        with open(ME_GROUP_FILE,"r",encoding="utf-8") as f: data=json.load(f)
        return {str(x) for x in data} if isinstance(data,list) else set()
    except Exception: return set()

def _save_me_group_file() -> None:
    _atomic_write(ME_GROUP_FILE,json.dumps(sorted(me_group_users),ensure_ascii=False),mode=0o600)

def load_chat_history_file(path: str) -> list:
    if not os.path.exists(path): return []
    try:
        with open(path,"r",encoding="utf-8") as f: data=json.load(f)
        return data if isinstance(data,list) else []
    except Exception: return []

def save_chat_history_file(path: str, history: list) -> None:
    _atomic_write(path,json.dumps(history[-CHAT_HISTORY_MAX:],ensure_ascii=False),mode=0o600)

chat_history_cache = load_chat_history_file(CHAT_HISTORY_FILE)[-CHAT_HISTORY_MAX:]
me_chat_history_cache = load_chat_history_file(ME_CHAT_HISTORY_FILE)[-CHAT_HISTORY_MAX:]

def _media_extension(mime: str) -> Optional[str]: return CHAT_ALLOWED_MEDIA.get(mime.lower())
def _safe_chat_filename(filename: str) -> str:
    base=os.path.basename(str(filename or "file")); base=re.sub(r"[^A-Za-z0-9._-]+","_",base); return base[:120] or "file"


def load_scripts_from_file() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(SCRIPTS_FILE):
        return {}
    try:
        with open(SCRIPTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_scripts_to_file():
    _atomic_write(SCRIPTS_FILE, json.dumps(scripts), mode=0o600)


users = load_users_from_file()
scripts = load_scripts_from_file()
me_group_users:Set[str]={u for u in _load_me_group_file() if u in users}
_save_me_group_file()

RESERVED_PATHS = {
    "", "home", "admin", "logs", "usernames", "blacklisted", "announcements",
    "ws", "secure", "dexfree", "dexchilli", "dexserverhop", "dexhub", "dexpaid",
    "dexautoroll", "dexcodesniper", "admin/stats", "admin/update", "admin/announcement", "admin/banner", "admin/chat/toggle", "admin/chat/clear", "admin/github/refresh", "admin/me-group", "admin/datadir", "favicon.ico", "robots.txt",
    "scripts", "banner", "github/refresh", "dexpaid/keys", "chat", "me-chat", "ws/chat", "ws/me-chat", "chat/media", "admin/me-group",
    "login", "logout", "auth", "auth/discord/callback", "admin/logout",
}
RESERVED_PATHS_LOWER = {p.lower() for p in RESERVED_PATHS}


def ensure_builtin_scripts():
    builtin = [
        ("DexFree", "dexfree", DEXFREE_FILE, DEFAULT_DEXFREE),
        ("DexChilli", "dexchilli", DEXCHILLI_FILE, DEFAULT_DEXCHILLI),
        ("DexServerHop", "dexserverhop", DEXSERVERHOP_FILE, DEFAULT_DEXSERVERHOP),
        ("DexHub", "dexhub", DEXHUB_FILE, DEFAULT_DEXHUB),
        ("DexAutoRoll", "dexautoroll", DEXAUTOROLL_FILE, DEFAULT_DEXAUTOROLL),
        ("DexCodeSniper", "dexcodesniper", DEXCODESNIPER_FILE, DEFAULT_DEXCODESNIPER),
    ]
    for name, slug, path, default in builtin:
        if slug not in scripts:
            scripts[slug] = {
                "name": name,
                "slug": slug,
                "code": load_file(path, default),
                "is_paid": False,
                "hwid_lock": False,
                "owner": "SYSTEM",
                "created_at": time.time(),
                "updated_at": time.time(),
                "keys": {},
                "last_key": "",
                "last_loadstring": "",
                "github_managed": True,
            }
        else:
            scripts[slug]["github_managed"] = True
    save_scripts_to_file()


ensure_builtin_scripts()

GLOBAL_HTTP_RATE_LIMIT = 200
GLOBAL_HTTP_RATE_WINDOW = 10.0


@app.middleware("http")
async def global_rate_limit_and_security_headers(request: Request, call_next):
    ip = _client_ip(request)

    is_bridge_request = request.url.path.startswith("/bridge/")

    if not is_bridge_request and rate_limited(
        ip, "global_http", max_requests=GLOBAL_HTTP_RATE_LIMIT, window_seconds=GLOBAL_HTTP_RATE_WINDOW
    ):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    )
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"

    media = response.headers.get("content-type", "")
    if "text/html" in media and getattr(response, "body", None):
        skin = b"""<style id="dex-global-skin">
:root{color-scheme:dark;--dex-bg:#060913;--dex-bg2:#0a1020;--dex-card:rgba(13,20,36,.88);--dex-card2:rgba(10,16,29,.92);--dex-line:#22304b;--dex-line2:#2c3d5e;--dex-text:#f4f7ff;--dex-muted:#91a0bb;--dex-accent:#7c5cff;--dex-cyan:#22d3ee;--dex-green:#35d07f;--dex-red:#ff6b81}
*{box-sizing:border-box}
html{background:var(--dex-bg)}
body{background:radial-gradient(900px 520px at 8% -10%,rgba(124,92,255,.20),transparent 62%),radial-gradient(760px 500px at 92% 0%,rgba(34,211,238,.12),transparent 58%),linear-gradient(145deg,#050812,#090f1d 55%,#060913)!important;color:var(--dex-text)!important;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif!important;min-height:100vh;letter-spacing:.005em}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:36px 36px;mask-image:linear-gradient(to bottom,black,transparent 78%);z-index:0}
body>*{position:relative;z-index:1}
a{color:#9db4ff!important;text-decoration:none;transition:.18s ease}a:hover{color:#d5ddff!important}
h1,h2,h3,h4{color:#f7f9ff!important;letter-spacing:-.025em}
p,small,.muted,.subtitle,.hint{color:var(--dex-muted)!important;line-height:1.65}
button,.btn,input[type=submit],input[type=button]{border-radius:12px!important;border:1px solid rgba(124,92,255,.35)!important;background:linear-gradient(135deg,#7657f4,#4d79ff)!important;color:#fff!important;font-weight:800!important;box-shadow:0 10px 28px rgba(70,80,220,.18);transition:transform .15s ease,box-shadow .15s ease,filter .15s ease;cursor:pointer}
button:hover,.btn:hover,input[type=submit]:hover,input[type=button]:hover{transform:translateY(-1px);filter:brightness(1.08);box-shadow:0 14px 34px rgba(70,80,220,.26)}
button:disabled,.btn:disabled{opacity:.55;transform:none;cursor:not-allowed}
input,textarea,select{border-radius:12px!important;border:1px solid var(--dex-line2)!important;background:rgba(5,10,19,.82)!important;color:#e8efff!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.025),0 8px 28px rgba(0,0,0,.12);outline:none}
input:focus,textarea:focus,select:focus{border-color:rgba(124,92,255,.8)!important;box-shadow:0 0 0 3px rgba(124,92,255,.12)!important}
.card,.panel,.container,.box,.resultbox,.result,.script-card,.admin-card,.section,.hero-card,.feature,.stat,.table-wrap,.auth-card{background:linear-gradient(145deg,var(--dex-card),var(--dex-card2))!important;border:1px solid rgba(86,108,150,.24)!important;border-radius:20px!important;box-shadow:0 24px 80px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.025)!important;backdrop-filter:blur(14px)}
.card:hover,.script-card:hover,.feature:hover{border-color:rgba(124,92,255,.38)!important}
pre,.code-box,.code,.output,.out{background:#050a13!important;border:1px solid var(--dex-line)!important;border-radius:14px!important;color:#cfe0ff!important}
table{border-collapse:separate!important;border-spacing:0!important;width:100%}th{background:#101a2d!important;color:#dce7ff!important}td{background:rgba(7,12,22,.65)!important;color:#b9c7df!important;border-color:#1d2a42!important}th:first-child{border-top-left-radius:10px}th:last-child{border-top-right-radius:10px}tr:last-child td:first-child{border-bottom-left-radius:10px}tr:last-child td:last-child{border-bottom-right-radius:10px}
hr{border:0!important;border-top:1px solid rgba(75,94,130,.25)!important}
.badge,.pill,.tag{border-radius:999px!important;background:rgba(124,92,255,.12)!important;border:1px solid rgba(124,92,255,.30)!important;color:#cfd5ff!important}
.status.ok,.success{color:var(--dex-green)!important}.status.error,.error{color:var(--dex-red)!important}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-track{background:#060a12}::-webkit-scrollbar-thumb{background:#243453;border-radius:999px;border:2px solid #060a12}::-webkit-scrollbar-thumb:hover{background:#354a73}
@media(max-width:1100px){.wrap,.container{width:min(100% - 32px,1180px)!important;margin-left:auto!important;margin-right:auto!important}.grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}.table-wrap{overflow-x:auto!important}.dn-home-inner{width:100%!important}.dn-hero{grid-template-columns:1fr!important}.dn-bottom{grid-template-columns:repeat(2,minmax(0,1fr))!important}}@media(max-width:700px){body{font-size:14px}.wrap,.container{width:min(100% - 24px,1180px)!important;margin-left:auto!important;margin-right:auto!important}.card,.panel,.container,.box,.resultbox,.result,.script-card,.admin-card,.section,.hero-card,.feature,.stat,.table-wrap,.auth-card{border-radius:16px!important}.grid{grid-template-columns:1fr!important}.copyrow{flex-direction:column!important}.actions{flex-direction:column!important;align-items:stretch!important}.dn-bottom{grid-template-columns:1fr!important}.dn-nav{flex-wrap:wrap}.dn-actions{flex-direction:column}.dn-actions a{width:100%}button,.btn,input[type=submit],input[type=button]{min-height:46px}}
</style>"""
        body = response.body
        if b"dex-global-skin" not in body:
            body = body.replace(b"</head>", skin + b"</head>", 1)
            response.body = body
            response.headers["content-length"] = str(len(body))
    return response


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    print(f"❌ Unhandled exception on {request.url.path}: {exc}")
    return PlainTextResponse("Something went wrong. Please try again.", status_code=500)


async def github_script_refresh_loop():
    while True:
        try:
            results = await refresh_all_github_scripts()
            failed = [name for name, result in results.items() if not result.get("ok")]
            if failed:
                print(f"[SCRIPTS] 60s refresh completed with failures: {', '.join(failed)}")
            else:
                print("[SCRIPTS] 60s refresh completed successfully.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SCRIPTS] background refresh error: {exc}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(rate_bucket_janitor())
    asyncio.create_task(username_cleanup_janitor())
    asyncio.create_task(github_script_refresh_loop())

current_wss: Optional[str] = None

SECURE_RATE_LIMIT = 20
SECURE_RATE_WINDOW = 10.0


@app.post("/secure")
async def set_wss(request: Request):
    global current_wss

    ip = _client_ip(request)
    if rate_limited(ip, "secure_post", max_requests=SECURE_RATE_LIMIT, window_seconds=SECURE_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("secure_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return JSONResponse({"error": "payload too large"}, status_code=413)

    raw_body = await request.body()
    if len(raw_body) > MAX_GENERIC_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    candidate = normalize_field(raw_body.decode("utf-8", errors="ignore").strip())

    if not candidate:
        return JSONResponse({"error": "empty"}, status_code=400)
    if _STRICT_SINGLE_LINE_PATTERN.search(candidate):
        return JSONResponse({"error": "invalid characters"}, status_code=400)
    if not WSS_URL_PATTERN.match(candidate):
        return JSONResponse({"error": "invalid format - expected a ws:// or wss:// URL"}, status_code=400)
    if contains_blocked_content(candidate):
        return JSONResponse({"error": "blocked content"}, status_code=400)

    current_wss = candidate
    return {"wss": current_wss}


@app.get("/secure")
async def get_wss(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "secure_get", max_requests=SECURE_RATE_LIMIT, window_seconds=SECURE_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("secure_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"wss": current_wss}


MAX_WS_CONNECTIONS_PER_IP = 5
WS_CONNECT_RATE_LIMIT = 20
WS_CONNECT_RATE_WINDOW = 60.0
WS_SEND_RATE_LIMIT = 30
WS_SEND_RATE_WINDOW = 10.0


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global sender_ws

    ip = _ws_client_ip(websocket)

    if await is_rate_limited("ws_sender_auth", ip):
        await websocket.close(code=4429)
        return

    if rate_limited(ip, "ws_connect", max_requests=WS_CONNECT_RATE_LIMIT, window_seconds=WS_CONNECT_RATE_WINDOW):
        await websocket.close(code=4429)
        return

    key_param = websocket.query_params.get("key", "")
    if not is_valid_key(key_param):
        await websocket.close(code=4401)
        return

    async with ws_count_lock:
        if ws_ip_connection_counts[ip] >= MAX_WS_CONNECTIONS_PER_IP:
            await websocket.close(code=4009)
            return
        ws_ip_connection_counts[ip] += 1

    await websocket.accept()

    role = "viewer"
    viewers.add(websocket)

    async with logs_lock:
        if stored_logs:
            last_entry = stored_logs[-1]
            try:
                await websocket.send_text(last_entry)
            except Exception:
                pass

    try:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break

            if len(msg) > MAX_GENERIC_BODY:
                continue

            text = msg.strip()
            if not text:
                continue

            if role == "viewer":
                if rate_limited(ip, "ws_sender_auth_attempt", max_requests=10, window_seconds=60.0):
                    continue
                if constant_time_eq(text, API_KEY):
                    role = "sender"
                    sender_ws = websocket
                    viewers.discard(websocket)
                    await clear_attempts("ws_sender_auth", ip)
                else:
                    await record_failed_attempt("ws_sender_auth", ip)
                continue

            if role == "sender":
                if rate_limited(ip, "ws_send", max_requests=WS_SEND_RATE_LIMIT, window_seconds=WS_SEND_RATE_WINDOW):
                    continue

                log_entry = text

                async with logs_lock:
                    stored_logs.append(log_entry)
                    if len(stored_logs) > MAX_STORED_LOGS:
                        del stored_logs[0: len(stored_logs) - MAX_STORED_LOGS]
                    append_log_to_file(log_entry)

                    dead = []
                    for v in list(viewers):
                        try:
                            await v.send_text(log_entry)
                        except Exception:
                            dead.append(v)

                    for d in dead:
                        viewers.discard(d)
            else:
                continue

    except WebSocketDisconnect:
        pass
    finally:
        if role == "viewer":
            viewers.discard(websocket)
        elif role == "sender" and sender_ws is websocket:
            sender_ws = None
        async with ws_count_lock:
            ws_ip_connection_counts[ip] -= 1
            if ws_ip_connection_counts[ip] <= 0:
                ws_ip_connection_counts.pop(ip, None)


LOGS_POST_RATE_LIMIT = 20
LOGS_POST_RATE_WINDOW = 10.0
LOGS_GET_RATE_LIMIT = 30
LOGS_GET_RATE_WINDOW = 10.0


@app.post("/logs")
async def post_logs(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "logs_post", max_requests=LOGS_POST_RATE_LIMIT, window_seconds=LOGS_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if reject_if_oversized(request, MAX_LOG_LEN):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_LOG_LEN:
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    msg = raw.decode(errors="ignore").strip()

    if not msg:
        return PlainTextResponse("EMPTY")

    if _CONTROL_CHAR_PATTERN.search(msg):
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)

    msg = normalize_field(msg)

    if contains_blocked_content(msg):
        return PlainTextResponse("REJECTED_BLOCKED_CONTENT", status_code=400)

    async with logs_lock:
        stored_logs.append(msg)
        if len(stored_logs) > MAX_STORED_LOGS:
            del stored_logs[0: len(stored_logs) - MAX_STORED_LOGS]
        append_log_to_file(msg)

        dead = []
        for v in list(viewers):
            try:
                await v.send_text(msg)
            except Exception:
                dead.append(v)

        for d in dead:
            viewers.discard(d)

    return PlainTextResponse("OK")


@app.get("/logs")
async def get_logs(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "logs_get", max_requests=LOGS_GET_RATE_LIMIT, window_seconds=LOGS_GET_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with logs_lock:
        return PlainTextResponse("\n".join(stored_logs))


USERNAME_POST_RATE_LIMIT = 8
USERNAME_POST_RATE_WINDOW = 10.0
USERNAME_GET_RATE_LIMIT = 30
USERNAME_GET_RATE_WINDOW = 10.0


@app.post("/usernames")
async def add_username(request: Request):
    ip = _client_ip(request)

    if rate_limited(ip, "username_post", max_requests=USERNAME_POST_RATE_LIMIT, window_seconds=USERNAME_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if await is_rate_limited("username_reject", ip):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    if reject_if_oversized(request, MAX_USERNAME_LEN + 16):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_USERNAME_LEN + 16:
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw_username = raw.decode(errors="ignore").strip()

    if not raw_username or len(raw_username) > MAX_USERNAME_LEN:
        return PlainTextResponse("EMPTY")

    if _ANY_WHITESPACE_PATTERN.search(raw_username):
        return PlainTextResponse("IGNORED_CONTAINS_WHITESPACE")

    if _CONTROL_CHAR_PATTERN.search(raw_username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)

    username = normalize_field(raw_username)

    if _ANY_WHITESPACE_PATTERN.search(username):
        return PlainTextResponse("IGNORED_CONTAINS_WHITESPACE")

    if not is_valid_username(username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_INVALID_OR_BLOCKED", status_code=400)

    if has_excessive_repetition(username):
        await record_failed_attempt("username_reject", ip)
        return PlainTextResponse("REJECTED_SPAM_PATTERN", status_code=400)

    should_restart = False

    async with lock:
        if username.lower() not in stored_usernames_lower:
            stored_usernames.add(username)
            stored_usernames_lower.add(username.lower())

        _purge_bad_usernames_locked()

        save_usernames_to_file(stored_usernames)

        if len(stored_usernames) >= MAX_STORED_USERNAMES:
            should_restart = True

    await clear_attempts("username_reject", ip)

    if should_restart:
        asyncio.create_task(schedule_restart())
        return PlainTextResponse("OK_LIMIT_REACHED_RESTARTING")

    return PlainTextResponse("OK")


@app.get("/usernames")
async def get_usernames(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "username_get", max_requests=USERNAME_GET_RATE_LIMIT, window_seconds=USERNAME_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    async with lock:
        return PlainTextResponse("\n".join(sorted(stored_usernames)))


BLACKLIST_POST_RATE_LIMIT = 15
BLACKLIST_POST_RATE_WINDOW = 10.0
BLACKLIST_GET_RATE_LIMIT = 30
BLACKLIST_GET_RATE_WINDOW = 10.0


@app.post("/blacklisted")
async def add_blacklisted(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "blacklist_post", max_requests=BLACKLIST_POST_RATE_LIMIT, window_seconds=BLACKLIST_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("blacklist_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode(errors="ignore").strip()

    if not username:
        return PlainTextResponse("EMPTY")

    async with blacklist_lock:
        if username not in blacklisted_usernames:
            blacklisted_usernames.add(username)
            save_blacklist_to_file(blacklisted_usernames)

    return PlainTextResponse("OK")


@app.post("/unblacklisted")
async def remove_blacklisted(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "blacklist_post", max_requests=BLACKLIST_POST_RATE_LIMIT, window_seconds=BLACKLIST_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("blacklist_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    username = raw.decode(errors="ignore").strip()

    if not username:
        return PlainTextResponse("EMPTY")

    async with blacklist_lock:
        if username in blacklisted_usernames:
            blacklisted_usernames.discard(username)
            save_blacklist_to_file(blacklisted_usernames)

    return PlainTextResponse("OK")


@app.get("/blacklisted")
async def get_blacklisted(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "blacklist_get", max_requests=BLACKLIST_GET_RATE_LIMIT, window_seconds=BLACKLIST_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    async with blacklist_lock:
        return PlainTextResponse("\n".join(sorted(blacklisted_usernames)))


ANNOUNCEMENT_POST_RATE_LIMIT = 10
ANNOUNCEMENT_POST_RATE_WINDOW = 10.0
ANNOUNCEMENT_GET_RATE_LIMIT = 60
ANNOUNCEMENT_GET_RATE_WINDOW = 10.0


@app.post("/announcements")
async def post_announcement(request: Request):
    global announcement_text, announcement_timestamp

    ip = _client_ip(request)
    if rate_limited(ip, "announcement_post", max_requests=ANNOUNCEMENT_POST_RATE_LIMIT, window_seconds=ANNOUNCEMENT_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("announcement_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD TOO LARGE", status_code=413)
    msg = normalize_field(raw.decode("utf-8", errors="ignore").strip())
    if len(msg) > MAX_BANNER_LEN:
        return PlainTextResponse("TOO_LONG", status_code=400)
    if _CONTROL_CHAR_PATTERN.search(msg):
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)
    async with announcement_lock:
        announcement_text = msg
        announcement_timestamp = time.time() if msg else 0.0
        save_announcement_to_file(announcement_text)
    await clear_attempts("announcement_auth", ip)
    return PlainTextResponse("OK")


@app.get("/announcements")
async def get_announcement(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "announcement_get", max_requests=ANNOUNCEMENT_GET_RATE_LIMIT, window_seconds=ANNOUNCEMENT_GET_RATE_WINDOW):
        return PlainTextResponse("", status_code=429)
    async with announcement_lock:
        return PlainTextResponse(announcement_text)


BANNER_POST_RATE_LIMIT = 10
BANNER_POST_RATE_WINDOW = 10.0
BANNER_GET_RATE_LIMIT = 60
BANNER_GET_RATE_WINDOW = 10.0


@app.post("/banner")
async def post_banner(request: Request):
    global banner_text

    ip = _client_ip(request)
    if rate_limited(ip, "banner_post", max_requests=BANNER_POST_RATE_LIMIT, window_seconds=BANNER_POST_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("banner_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return PlainTextResponse("PAYLOAD_TOO_LARGE", status_code=413)

    msg = normalize_field(raw.decode("utf-8", errors="ignore").strip())

    if len(msg) > MAX_BANNER_LEN:
        return PlainTextResponse("TOO_LONG", status_code=400)

    if _CONTROL_CHAR_PATTERN.search(msg):
        return PlainTextResponse("REJECTED_INVALID_CHARACTERS", status_code=400)

    async with banner_lock:
        banner_text = msg
        save_banner_to_file(banner_text)

    await clear_attempts("banner_auth", ip)
    return PlainTextResponse("OK")


@app.get("/banner")
async def get_banner(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "banner_get", max_requests=BANNER_GET_RATE_LIMIT, window_seconds=BANNER_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)
    async with banner_lock:
        return PlainTextResponse(banner_text)


DEXPAID_KEYS_RATE_LIMIT = 10
DEXPAID_KEYS_RATE_WINDOW = 60.0


@app.post("/dexpaid/keys")
async def create_dexpaid_key(request: Request):
    global last_generated_paid_key, last_generated_paid_loadstring

    ip = _client_ip(request)
    if rate_limited(ip, "dexpaid_keys_post", max_requests=DEXPAID_KEYS_RATE_LIMIT, window_seconds=DEXPAID_KEYS_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("dexpaid_keys_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if reject_if_oversized(request, MAX_GENERIC_BODY):
        return JSONResponse({"error": "payload too large"}, status_code=413)

    raw = await request.body()
    if len(raw) > MAX_GENERIC_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    hours = parse_duration_hours(raw.decode(errors="ignore").strip())
    if hours is None:
        return JSONResponse({"error": f"invalid duration (0 < hours <= {MAX_KEY_DURATION_HOURS})"}, status_code=400)

    async with dexpaid_keys_lock:
        cleanup_expired_paid_keys()
        new_key = generate_paid_key(20)
        expiry = time.time() + hours * 3600.0
        dexpaid_keys[new_key] = expiry
        save_dexpaid_keys_to_file(dexpaid_keys)
        last_generated_paid_key = new_key
        last_generated_paid_loadstring = (
            f'loadstring(game:HttpGet("{BASE_URL}/dexpaid?key={new_key}"))()'
        )

    return JSONResponse({
        "key": new_key,
        "expires_at": expiry,
        "loadstring": last_generated_paid_loadstring,
    })


GITHUB_REFRESH_RATE_LIMIT = 5
GITHUB_REFRESH_RATE_WINDOW = 60.0


@app.post("/github/refresh")
async def refresh_github(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "github_refresh_post", max_requests=GITHUB_REFRESH_RATE_LIMIT, window_seconds=GITHUB_REFRESH_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    api_key = request.headers.get("X-Api-Key", "")
    if not is_valid_key(api_key):
        await record_failed_attempt("github_refresh_auth", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    await force_refresh_github_cache()
    return JSONResponse({"ok": True, "message": "GitHub script cache cleared - next request re-fetches."})


@app.post("/admin/github/refresh")
async def admin_refresh_github(request: Request):
    ip = _client_ip(request)
    if not require_admin_session(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if rate_limited(ip, "admin_github_refresh_post", max_requests=GITHUB_REFRESH_RATE_LIMIT, window_seconds=GITHUB_REFRESH_RATE_WINDOW):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    if not github_configured() and not FIXED_RAW_SCRIPT_URLS:
        return JSONResponse({"error": "No remote script sources configured"}, status_code=400)

    results = await refresh_all_github_scripts()
    all_ok = all(r.get("ok") for r in results.values())
    return JSONResponse({"ok": all_ok, "results": results})


INDEX_RATE_LIMIT = 30
INDEX_RATE_WINDOW = 10.0


@app.get("/favicon.ico")
async def favicon_ico():
    return RedirectResponse(url=DEX_FAVICON_URL, status_code=307)


@app.get("/")
async def index(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "index_get", max_requests=INDEX_RATE_LIMIT, window_seconds=INDEX_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    username = get_logged_in_user(request)
    error_message = discord_error_message(request.query_params.get("discord_error", ""))

    if username:
        auth_nav_html = (
            '<span class="dn-chrome-status" style="margin-left:2px;">'
            f'<i></i> Signed in as {html.escape(username)}</span>'
        )
    elif discord_oauth_configured():
        auth_nav_html = '<a href="/login">Sign in</a>'
    else:
        auth_nav_html = '<a href="/home">Sign in</a>'

    notice_html = f'<div class="dn-notice">{html.escape(error_message)}</div>' if error_message else ""

    html_page = f"""
    <!doctype html>
    <html lang="en">
    <head><link rel="icon" type="image/webp" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"><link rel="shortcut icon" type="image/webp" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"><link rel="apple-touch-icon" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536">
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
      <meta name="theme-color" content="#000000">
      <meta name="color-scheme" content="dark">
      <title>DexNotifier — Lua Infrastructure</title>
      <style>
        .dn-home{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:34px 18px}}
        .dn-home-inner{{width:min(1120px,100%);position:relative}}
        .dn-nav{{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:74px}}
        .dn-brand{{display:flex;align-items:center;gap:12px;font-weight:950;letter-spacing:-.03em;font-size:18px}}
        .dn-logo{{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,#8b5cf6,#22d3ee);box-shadow:0 12px 40px rgba(99,102,241,.35);animation:dnFloat 4s ease-in-out infinite}}
        .dn-logo span{{font-weight:1000;color:white}}
        .dn-navlinks{{display:flex;gap:9px;flex-wrap:wrap}}
        .dn-navlinks a{{padding:10px 14px;border:1px solid rgba(148,163,184,.12)!important;background:rgba(15,20,38,.55);border-radius:12px;color:#cbd5e1!important;font-size:13px;font-weight:800}}
        .dn-hero{{display:grid;grid-template-columns:1.18fr .82fr;gap:42px;align-items:center}}
        .dn-eyebrow{{display:inline-flex;align-items:center;gap:8px;color:#c4b5fd;font-weight:900;font-size:12px;letter-spacing:.15em;text-transform:uppercase}}
        .dn-dot{{width:7px;height:7px;border-radius:50%;background:#34d399;box-shadow:0 0 18px #34d399;animation:dnPulse 1.8s infinite}}
        .dn-hero h1{{font-size:clamp(52px,8vw,94px);line-height:.92;letter-spacing:-.075em;margin:18px 0 22px;max-width:800px}}
        .dn-hero h1 span{{background:linear-gradient(100deg,#fff 15%,#c4b5fd 48%,#67e8f9 92%);-webkit-background-clip:text;background-clip:text;color:transparent}}
        .dn-hero p{{max-width:680px;font-size:18px;line-height:1.7;color:#9aa6c0!important}}
        .dn-actions{{display:flex;gap:12px;margin-top:28px;flex-wrap:wrap}}
        .dn-actions a{{display:inline-flex;align-items:center;justify-content:center;padding:14px 19px;border-radius:14px!important;font-weight:900}}
        .dn-primary{{background:linear-gradient(135deg,#8b5cf6,#6366f1)!important;color:white!important;box-shadow:0 18px 45px rgba(99,102,241,.27)}}
        .dn-secondary{{background:rgba(17,24,39,.65);border:1px solid rgba(148,163,184,.16)!important;color:#dbe4f5!important}}
        .dn-side{{padding:24px;border-radius:28px;background:linear-gradient(145deg,rgba(18,25,48,.82),rgba(7,11,22,.82));border:1px solid rgba(148,163,184,.14);box-shadow:0 30px 90px rgba(0,0,0,.38);backdrop-filter:blur(20px)}}
        .dn-side-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}}
        .dn-status{{display:flex;align-items:center;gap:7px;color:#a7f3d0;font-size:12px;font-weight:900}}
        .dn-feature{{display:flex;gap:13px;padding:15px 0;border-top:1px solid rgba(148,163,184,.09)}}
        .dn-feature:first-of-type{{border-top:0}}
        .dn-icon{{width:38px;height:38px;flex:0 0 38px;border-radius:12px;display:grid;place-items:center;background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.2);color:#c4b5fd;font-weight:950}}
        .dn-feature b{{display:block;font-size:14px;margin-bottom:4px}}
        .dn-feature span{{font-size:12px;color:#7f8ba5}}
        .dn-bottom{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:58px}}
        .dn-mini{{padding:18px;border-radius:18px;background:rgba(12,17,32,.65);border:1px solid rgba(148,163,184,.1)}}
        .dn-mini strong{{display:block;font-size:14px;margin-bottom:6px}}.dn-mini span{{color:#77839c;font-size:12px;line-height:1.5}}
        .dn-notice{{margin:0 0 26px;padding:13px 16px;border-radius:14px;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.25);color:#fecdd3;font-size:13px;font-weight:700;text-align:center}}
        @media(max-width:800px){{.dn-nav{{margin-bottom:45px;align-items:flex-start}}.dn-navlinks{{display:none}}.dn-hero{{grid-template-columns:1fr;gap:24px}}.dn-hero h1{{font-size:clamp(50px,15vw,76px)}}.dn-hero p{{font-size:16px}}.dn-bottom{{grid-template-columns:1fr}}.dn-side{{padding:19px}}}}
      </style>
    </head>
    <body class="dn-base-home">
      <main class="dn-home"><div class="dn-home-inner">
        <nav class="dn-nav">
          <div class="dn-brand"><div class="dn-logo"><span>D</span></div><span>DexNotifier</span></div>
          <div class="dn-navlinks"><a href="/obfuscate">Obfustucate</a><a href="/chat">Chat</a><a href="/scripts">Scripts</a><a href="/home">Home</a><a href="/admin">Admin</a>{auth_nav_html}</div>
        </nav>
        {notice_html}
        <section class="dn-hero">
          <div>
            <div class="dn-eyebrow"><i class="dn-dot"></i> DexNotifier infrastructure</div>
            <h1>Build. Protect.<br><span>Ship Lua.</span></h1>
            <p>A modern control layer for your Lua loaders, protected payloads, script endpoints and private administration tools — all from one fast backend.</p>
            <div class="dn-actions"><a class="dn-primary" href="/obfuscate">Open Obfustucate →</a><a class="dn-secondary" href="/chat">Open Chat</a><a class="dn-secondary" href="/scripts">Browse scripts</a></div>
          </div>
          <aside class="dn-side">
            <div class="dn-side-head"><strong>System overview</strong><span class="dn-status"><i class="dn-dot"></i> Online</span></div>
            <div class="dn-feature"><div class="dn-icon">01</div><div><b>Lua protection</b><span>Turn source into a protected, ready-to-load payload.</span></div></div>
            <div class="dn-feature"><div class="dn-icon">02</div><div><b>Script delivery</b><span>Centralized loader endpoints with copy-ready loadstrings.</span></div></div>
            <div class="dn-feature"><div class="dn-icon">03</div><div><b>Private control</b><span>Administration and diagnostics stay behind authentication.</span></div></div>
          </aside>
        </section>
        <section class="dn-bottom">
          <div class="dn-mini"><strong>Obfustucate</strong><span>Clean browser UI for protected Lua payload generation.</span></div>
          <div class="dn-mini"><strong>Chat</strong><span>Live community chat with image and video sharing for signed-in accounts.</span></div>
          <div class="dn-mini"><strong>Scripts</strong><span>Browse the public loader catalog without exposing internal routes.</span></div>
          <div class="dn-mini"><strong>Admin</strong><span>Private control center for your existing backend state.</span></div>
        </section>
      </div></main>
    </body></html>
    """
    return HTMLResponse(html_page)


SCRIPTS_GET_RATE_LIMIT = 30
SCRIPTS_GET_RATE_WINDOW = 10.0

SCRIPTS_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><link rel="icon" type="image/webp" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"><link rel="shortcut icon" type="image/webp" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"><link rel="apple-touch-icon" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536">
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>DEX SCRIPTS</title>
    <style>
        :root {{
            --bg: #050509; --accent1: #4fc3f7; --accent2: #7c4dff;
            --accent3: #ff5252; --accent4: #00e676; --border: #1c1c24;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            background:
                radial-gradient(circle at top left, #202040 0, #050509 40%, #000000 100%),
                linear-gradient(135deg, rgba(79,195,247,0.08), rgba(255,82,82,0.08));
            color: #e6e6e6;
        }}
        .wrap {{ max-width: 1180px; margin: 0 auto; padding: 48px 22px 60px; }}
        .hero {{ text-align: center; margin-bottom: 34px; }}
        .hero h1 {{
            margin: 0; font-size: 46px; font-weight: 800; letter-spacing: 0.08em;
            background: linear-gradient(135deg, var(--accent1), var(--accent2));
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }}
        .hero p {{ margin: 10px 0 0; color: #9a9ab0; font-size: 15px; }}
        .hero .pills {{ margin-top: 16px; }}
        .pill {{
            display: inline-block; padding: 5px 14px; border-radius: 999px; font-size: 12px;
            background: rgba(79,195,247,0.14); border: 1px solid rgba(79,195,247,0.35);
            color: #e6f7ff; margin: 0 4px;
        }}
        .pill.green {{ background: rgba(0,230,118,0.14); border-color: rgba(0,230,118,0.35); color: #e6fff3; }}
        .pill.purple {{ background: rgba(124,77,255,0.14); border-color: rgba(124,77,255,0.35); color: #f0e6ff; }}
        .banner {{
            max-width: 900px; margin: 0 auto 34px; padding: 14px 20px; border-radius: 14px;
            background: linear-gradient(135deg, rgba(124,77,255,0.16), rgba(79,195,247,0.16));
            border: 1px solid rgba(124,77,255,0.35); color: #f2f2ff; font-size: 14px;
            text-align: center; box-shadow: 0 12px 30px rgba(0,0,0,0.35);
        }}
        .grid {{
            display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 22px;
        }}
        .script-card {{
            background: linear-gradient(150deg, rgba(16,16,24,0.96), rgba(9,9,15,0.96));
            border: 1px solid rgba(79,195,247,0.16); border-radius: 18px; padding: 22px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.55);
            position: relative; overflow: hidden; transition: transform 0.15s ease, border-color 0.15s ease;
        }}
        .script-card:hover {{ transform: translateY(-3px); border-color: rgba(79,195,247,0.4); }}
        .script-card::before {{
            content: ""; position: absolute; inset: -40% -40% auto auto; width: 160px; height: 160px;
            background: radial-gradient(circle, rgba(79,195,247,0.18), transparent 70%); pointer-events: none;
        }}
        .script-card-header {{
            display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;
        }}
        .script-card-header h2 {{ margin: 0; font-size: 19px; letter-spacing: 0.01em; }}
        .tagline {{ color: #9a9ab0; font-size: 13px; margin: 4px 0 14px; }}
        .endpoint-row {{ font-size: 12px; color: #8a8aa0; margin-bottom: 12px; }}
        .endpoint-row code {{
            background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 6px; color: #c9c9e0;
        }}
        .code-row {{
            display: flex; align-items: center; gap: 10px;
            background: rgba(8,8,13,0.95); border: 1px solid #262636; border-radius: 12px;
            padding: 12px 12px; overflow: hidden;
        }}
        .code-row code {{
            flex: 1; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 12.5px; color: #9eff9e; white-space: nowrap; overflow-x: auto;
        }}
        .copy-btn {{
            flex-shrink: 0; border: none; border-radius: 999px; padding: 8px 16px; font-weight: 600;
            font-size: 12px; cursor: pointer; color: #050509;
            background: linear-gradient(135deg, var(--accent1), var(--accent2));
            transition: opacity 0.15s ease;
        }}
        .copy-btn:hover {{ opacity: 0.85; }}
        .copy-btn.copied {{ background: linear-gradient(135deg, var(--accent4), #00c853); }}
        .paid-note {{
            max-width: 900px; margin: 34px auto 0; text-align: center; font-size: 13px; color: #8a8aa0;
        }}
        .paid-note a {{ color: var(--accent1); text-decoration: none; }}
        .paid-note a:hover {{ text-decoration: underline; }}
        footer {{ text-align: center; margin-top: 46px; font-size: 12px; color: #5c5c70; }}
    </style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <h1>DEX SCRIPTS</h1>
            <p>Every free Dex loader, ready to copy into your executor.</p>
            <div class="pills">
                <span class="pill green">Free</span>
                <span class="pill">No key required</span>
                <span class="pill purple">Always up to date</span>
            </div>
        </div>
        {{banner_html}}
        <div class="grid">
            {{cards}}
        </div>
        <p class="paid-note">Looking for the paid script? Head to <a href="/dexpaid?key=YOUR_KEY">/dexpaid</a> with your key instead.</p>
        <footer>Dex API</footer>
    </div>
    <script>
        function copyScript(id, btn) {{
            const el = document.getElementById('code-' + id);
            if (!el) return;
            const text = el.textContent;
            const done = () => {{
                const original = btn.textContent;
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(() => {{
                    btn.textContent = original;
                    btn.classList.remove('copied');
                }}, 1500);
            }};
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
            }} else {{
                fallbackCopy(text, done);
            }}
        }}
        function fallbackCopy(text, done) {{
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try {{ document.execCommand('copy'); }} catch (e) {{}}
            document.body.removeChild(ta);
            done();
        }}
    </script>
</body>
</html>
"""


def build_scripts_page_html() -> str:
    banner_html = ""
    if banner_text:
        banner_html = f'<div class="banner">{html.escape(banner_text)}</div>'

    cards = ""
    for name, meta in FIXED_SCRIPTS.items():
        if name == "dexpaid":
            continue
        endpoint = f"{BASE_URL}/{name}"
        loadstring = f'loadstring(game:HttpGet("{html.escape(endpoint)}"))()'
        tagline = SCRIPT_TAGLINES.get(name, "")
        cards += f"""
        <div class="script-card">
            <div class="script-card-header">
                <h2>{html.escape(meta['label'])}</h2>
                <span class="pill green">Free</span>
            </div>
            <p class="tagline">{html.escape(tagline)}</p>
            <p class="endpoint-row">Endpoint: <code>/{html.escape(name)}</code></p>
            <div class="code-row">
                <code id="code-{html.escape(name)}">{loadstring}</code>
                <button class="copy-btn" onclick="copyScript('{html.escape(name)}', this)">Copy</button>
            </div>
        </div>
        """

    return SCRIPTS_PAGE_HTML.format(banner_html=banner_html, cards=cards)


@app.get("/scripts")
async def scripts_page(request: Request):
    ip = _client_ip(request)
    if rate_limited(ip, "scripts_get", max_requests=SCRIPTS_GET_RATE_LIMIT, window_seconds=SCRIPTS_GET_RATE_WINDOW):
        return PlainTextResponse("RATE_LIMITED", status_code=429)

    async with banner_lock:
        page = build_scripts_page_html()
    return HTMLResponse(page)


# ─────────────────────────────────────────────────────────────────────────────
# NOTE: The HOME, ADMIN, DISCORD OAUTH, LOADER ENDPOINTS, OBFUSCATOR,
# CHAT, BRIDGE, and GAME-CHAT sections below are identical to the original
# source. Only @app.post("/obfuscate") / obfuscate_api has been changed.
# To keep this file at a manageable size for the diff, the remainder of
# the original source follows verbatim from this point.
#
# IMPORTANT: In your actual deployment, paste everything from the original
# main.py that comes after the /scripts endpoint here — starting from
# HOME_BASE_HTML through to the final `if __name__ == "__main__":` block.
# The ONLY function that has changed is obfuscate_api (shown below).
# ─────────────────────────────────────────────────────────────────────────────

# [ ... all HOME, ADMIN, DISCORD OAUTH, LOADER, OBFUSCATOR page/helpers ... ]
# [ ... PASTE THE ORIGINAL FILE CONTENT HERE from HOME_BASE_HTML onward  ... ]
# [ ... replacing ONLY the @app.post("/obfuscate") function body          ... ]

# ─────────────────────────────────────────────────────────────────────────────
# THE ONLY CHANGED FUNCTION — drop this in place of the old obfuscate_api:
# ─────────────────────────────────────────────────────────────────────────────

OBF_RATE_LIMIT = 8
OBF_RATE_WINDOW = 60.0
OBF_MAX_SOURCE = 16 * 1024 * 1024

OBF_PAGE = r"""<!doctype html>
<html lang="en"><head><link rel="icon" type="image/webp" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"><link rel="shortcut icon" type="image/webp" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536"><link rel="apple-touch-icon" href="https://cdn.discordapp.com/icons/1505354277848219758/a6a84873eb83095e937b0051df49f5dc.webp?size=1536">
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070a12"><title>Obfustucate — DexNotifier</title>
<style>
*{box-sizing:border-box}html,body{width:100%;min-width:0}body{margin:0!important;color:#f8fafc;display:block!important;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#050713;overflow-x:hidden}
.page{display:flex!important;flex-direction:column!important;align-items:center!important;width:100%!important;max-width:none!important;min-height:100vh;padding:28px clamp(14px,3vw,42px) 70px;position:relative;margin:0 auto!important;text-align:initial!important}.page:before{content:"";position:fixed;inset:-20%;background:radial-gradient(circle at 20% 10%,rgba(124,92,246,.24),transparent 28%),radial-gradient(circle at 85% 20%,rgba(34,211,238,.14),transparent 24%);filter:blur(20px);animation:aurora 12s ease-in-out infinite alternate;pointer-events:none}.shell{display:block!important;width:100%!important;max-width:1320px!important;margin:0 auto!important;position:relative!important}.nav{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:62px}.brand{display:flex;align-items:center;gap:11px;font-weight:950}.logo{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,#8b5cf6,#22d3ee);box-shadow:0 15px 45px rgba(99,102,241,.35);animation:float 4s ease-in-out infinite}.navlinks{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.navlinks a{padding:10px 13px;border-radius:12px;border:1px solid rgba(148,163,184,.12);background:rgba(12,17,33,.65);color:#cbd5e1;text-decoration:none;font-size:13px;font-weight:800}.hero{text-align:center;max-width:850px;margin:0 auto 34px;width:100%}.eyebrow{display:inline-flex;align-items:center;gap:8px;color:#c4b5fd;text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:950}.live{width:7px;height:7px;border-radius:50%;background:#34d399;box-shadow:0 0 16px #34d399}.hero h1{font-size:clamp(54px,9vw,92px);line-height:.92;letter-spacing:-.075em;margin:17px 0 18px}.hero h1 span{background:linear-gradient(100deg,#fff,#c4b5fd 48%,#67e8f9);-webkit-background-clip:text;background-clip:text;color:transparent}.hero p{margin:auto;max-width:670px;color:#96a2bb;font-size:16px;line-height:1.7}.workspace{display:block;width:100%!important;max-width:1320px!important;margin:36px auto 0!important;padding:18px;border:1px solid rgba(148,163,184,.14);border-radius:28px;background:linear-gradient(145deg,rgba(15,21,40,.86),rgba(7,11,22,.86));box-shadow:0 35px 100px rgba(0,0,0,.45);backdrop-filter:blur(22px);align-self:center}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:3px 4px 15px}.traffic{display:flex;gap:7px}.traffic i{width:9px;height:9px;border-radius:50%;background:#334155}.traffic i:nth-child(1){background:#fb7185}.traffic i:nth-child(2){background:#fbbf24}.traffic i:nth-child(3){background:#34d399}.toolbar-title{font-size:12px;color:#8793ac;font-weight:900;letter-spacing:.08em;text-transform:uppercase}.editor-card{padding:18px;border-radius:20px;background:rgba(3,7,16,.72);border:1px solid rgba(148,163,184,.12)}.label{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:13px;font-weight:900}.hint{color:#69758d;font-weight:700}.editor{display:block;width:100%!important;min-height:460px;resize:vertical;border:1px solid #24314a;border-radius:16px;background:#040811;color:#dbeafe;padding:18px;font:14px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;outline:none;transition:.25s}.editor:focus{border-color:#8b5cf6;box-shadow:0 0 0 4px rgba(139,92,246,.11),0 0 45px rgba(139,92,246,.08)}.actionbar{display:flex;align-items:center;gap:12px;margin-top:14px}.go{flex:1;min-height:54px;border:0;border-radius:15px;color:white;font-weight:950;font-size:14px;cursor:pointer;background:linear-gradient(110deg,#8b5cf6,#6366f1,#22d3ee);background-size:200% 100%;box-shadow:0 16px 45px rgba(99,102,241,.25);transition:.25s}.go:hover{transform:translateY(-2px);background-position:100% 0}.go:disabled{opacity:.65;cursor:wait;transform:none}.status{min-width:120px;text-align:right;color:#77839b;font-size:12px;font-weight:800}.status.ok{color:#6ee7b7}.status.error{color:#fb7185}.results{display:grid;gap:14px;margin-top:14px}.result{padding:17px;border-radius:19px;background:rgba(4,8,17,.78);border:1px solid rgba(148,163,184,.12)}.result-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.result-head strong{font-size:13px}.result-head span{font-size:11px;color:#64748b}.copyrow{display:flex;gap:9px}.out{flex:1;min-width:0;min-height:74px;max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-word;padding:13px;border:1px solid #1f2b41;border-radius:13px;background:#03070f;color:#cfe0ff;font:12px/1.55 ui-monospace,monospace}.copy{min-width:86px;border:1px solid rgba(148,163,184,.14);border-radius:13px;background:#11192b;color:white;font-weight:900;cursor:pointer}.footer{text-align:center;color:#536078;font-size:11px;margin-top:20px}
@keyframes aurora{from{transform:translate3d(-2%,0,0) scale(1)}to{transform:translate3d(2%,2%,0) scale(1.06)}}@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@media(max-width:1100px){.page{padding-left:20px;padding-right:20px}.shell{max-width:100%!important}.workspace{width:100%!important}.hero{max-width:780px}.nav{margin-bottom:48px}}@media(max-width:900px){.page{width:100%!important;max-width:none!important;padding:18px 14px 45px}.shell{width:100%!important;max-width:none!important}.nav{margin-bottom:38px}.navlinks{display:none}.hero{width:100%;padding:0 4px}.hero h1{font-size:clamp(48px,15vw,72px)}.hero p{font-size:14px;max-width:620px}.workspace{width:100%!important;max-width:100%!important;margin:26px auto 0!important;padding:11px;border-radius:22px}.editor-card{padding:12px}.editor{min-height:300px;font-size:12px}.actionbar{flex-direction:column;align-items:stretch}.go{width:100%}.status{text-align:center;min-width:0}.copyrow{flex-direction:column}.copy{width:100%;min-height:44px}.toolbar{padding-bottom:11px}}@media(max-width:520px){.page{padding:14px 10px 34px}.brand{font-size:14px}.logo{width:38px;height:38px;border-radius:12px}.hero h1{font-size:clamp(42px,16vw,60px)}.hero p{font-size:13px}.workspace{padding:9px;border-radius:19px}.editor{min-height:260px;padding:14px}.result{padding:13px}.result-head{align-items:flex-start;flex-direction:column;gap:4px}.footer{font-size:10px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head>
<body><main class="page"><div class="shell">
<nav class="nav"><div class="brand"><div class="logo">D</div><span>DexNotifier</span></div><div class="navlinks"><a href="/">Home</a><a href="/scripts">Scripts</a><a href="/home">Dashboard</a></div></nav>
<section class="hero"><div class="eyebrow"><i class="live"></i> Lua protection tool</div><h1><span>Obfustucate</span></h1><p>Paste your raw Lua source below. DexNotifier generates the protected payload you can copy.</p></section>
<section class="workspace"><div class="toolbar"><div class="traffic"><i></i><i></i><i></i></div><div class="toolbar-title">Protected Lua workspace</div><div style="width:39px"></div></div>
<div class="editor-card"><div class="label"><span>Source</span><span class="hint">Lua · UTF-8</span></div><textarea id="source" class="editor" spellcheck="false" placeholder="-- paste your Lua source here"></textarea><div class="actionbar"><button id="go" class="go">Obfustucate Lua</button><span id="status" class="status">Ready</span></div></div>
<div id="results" class="results" style="display:none"><div class="result"><div class="result-head"><strong>Protected payload</strong><span>complete Lua file</span></div><div class="copyrow"><div id="payload" class="out"></div><button class="copy" data-copy="payload">Copy</button></div></div></div>
</section><div class="footer">DexNotifier · Obfustucate · Protected workspace</div></div></main>
<script>
const $=id=>document.getElementById(id),status=$('status');
async function copyText(text,button){try{if(navigator.clipboard)await navigator.clipboard.writeText(text);else{const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove()}const old=button.textContent;button.textContent='Copied';setTimeout(()=>button.textContent=old,1000)}catch(e){button.textContent='Copy failed';setTimeout(()=>button.textContent='Copy',1000)}}
$('go').onclick=async()=>{const source=$('source').value;if(!source.trim()){status.textContent='Paste Lua source first';status.className='status error';return}$('go').disabled=true;status.textContent='Protecting, Wait Patiently...';status.className='status';try{const r=await fetch('/obfuscate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source})});const d=await r.json();if(!r.ok)throw new Error(d.error||'Obfustucation failed');$('payload').textContent=d.payload||'';$('results').style.display='grid';status.textContent='Complete';status.className='status ok';$('results').scrollIntoView({behavior:'smooth',block:'nearest'})}catch(e){status.textContent=e.message;status.className='status error'}finally{$('go').disabled=false}};
document.querySelectorAll('.copy').forEach(b=>b.addEventListener('click',()=>copyText($(b.dataset.copy).textContent,b)));
</script></body></html>"""


@app.get("/obfuscate")
async def obfuscate_page():
    return HTMLResponse(OBF_PAGE)


@app.post("/obfuscate")
async def obfuscate_api(request: Request):
    """Call the DEX Obfuscator V8 API directly with a POST request, then wrap
    the returned payload with the Dex header and spacing before serving it.
    No bridge jobs or browser workers are involved.

    Required Railway env var: DEX_OBF_API_URL — the full URL of the
    DEX Obfuscator V8 /obfuscate endpoint, e.g.
      https://your-obfuscator.up.railway.app/obfuscate
    """
    ip = _client_ip(request)
    if rate_limited(ip, "obfustucate", OBF_RATE_LIMIT, OBF_RATE_WINDOW):
        return JSONResponse({"error": "Too many obfuscation requests. Try again shortly."}, status_code=429)
    if reject_if_oversized(request, OBF_MAX_SOURCE + 32 * 1024):
        return JSONResponse({"error": "Source is too large."}, status_code=413)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Expected JSON with source."}, status_code=400)
    source = body.get("source", "") if isinstance(body, dict) else ""
    if not isinstance(source, str) or not source.strip():
        return JSONResponse({"error": "Lua source is empty."}, status_code=400)
    if len(source.encode("utf-8")) > OBF_MAX_SOURCE:
        return JSONResponse({"error": "Source is too large (16 MB maximum)."}, status_code=413)

    # DEX Obfuscator V8 API endpoint — set DEX_OBF_API_URL in Railway env vars.
    obf_api_url = os.environ.get("DEX_OBF_API_URL", "").strip()
    if not obf_api_url:
        return JSONResponse(
            {"error": "DEX Obfuscator API is not configured on this deployment."},
            status_code=503,
        )

    # V8 settings — all protection layers enabled.
    obf_settings = {
        "encryptStrings": True,
        "proxifyLocals": True,
        "proxifyFunctions": True,
        "antiTamper": True,
        "controlFlowFlattening": True,
        "isLuauRuntime": False,
        "loaderVMDepth": 3,
    }

    def _call_obf_api():
        payload_bytes = json.dumps({"source": source, "settings": obf_settings}).encode("utf-8")
        req = urllib.request.Request(
            obf_api_url,
            data=payload_bytes,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "DexNotifier/4.5")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        result = await asyncio.to_thread(_call_obf_api)
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        print(f"[DEX_OBF_API] HTTP {exc.code}: {body_text}")
        return JSONResponse(
            {"error": "Please Make Sure Your Source Is Syntax Error Free"},
            status_code=502,
        )
    except Exception as exc:
        print(f"[DEX_OBF_API] request failed: {exc}")
        return JSONResponse(
            {"error": "DEX Obfuscator is unavailable. Try again shortly."},
            status_code=502,
        )

    if not isinstance(result, dict) or str(result.get("status", "")).lower() != "success":
        err = str(
            result.get("error") or result.get("message") or "DEX Obfuscator returned an error."
        )[:400]
        return JSONResponse({"error": err}, status_code=502)

    obf_result = str(result.get("result") or "").strip()
    if not obf_result:
        return JSONResponse(
            {"error": "DEX Obfuscator returned an empty payload."},
            status_code=502,
        )

    # Normalise line endings from the upstream service.
    obf_result = (
        obf_result
        .replace("\ufeff", "", 1)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    # Replace any upstream runtime-error strings with the Dex brand.
    obf_result = re.sub(
        r"error\(\s*(['\"])runtime error\1\s*\)",
        "error('DEX: Tamper Detected')",
        obf_result,
        flags=re.IGNORECASE,
    )

    if not obf_result.startswith("return"):
        return JSONResponse(
            {"error": "Please Make Sure Your Source Is Syntax Error Free"},
            status_code=502,
        )

    dex_header = "-- This file was protected using Dex Obfustucator v4.5 [.gg/dexfinder]"

    # Final output format:
    #   line 1: Dex header comment
    #   line 2: blank
    #   line 3+: obfuscated payload from DEX Obfuscator
    final_payload = dex_header + "\n\n" + obf_result

    if len(final_payload.encode("utf-8")) > 3 * 1024 * 1024:
        return JSONResponse(
            {"error": "Final protected payload exceeds the 3 MB output limit."},
            status_code=413,
        )

    return JSONResponse({"ok": True, "payload": final_payload})


# -----------------------------
# RUN
# -----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
