# app.py — Imago (PT-BR) • PT-BR, DOCX/PDF ok, Ping, Cancelamento, ETA, Regenerate
import os
import re
import time
import base64
import uuid
import threading
import random
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from PIL import Image
from urllib.parse import urlparse
import requests


from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
from dotenv import load_dotenv

# ============== Setup básico ==============
load_dotenv()

# ATENÇÃO: Verifique se 'template_folder="templates"' está correto
app = Flask(__name__, static_folder="static", template_folder="templates")


def static_url_with_buster(fpath: str) -> str:
    try:
        static_root = app.static_folder if hasattr(app, "static_folder") and app.static_folder else os.path.join(app.root_path, "static")
        rel_filename = os.path.relpath(fpath, static_root).replace("\\", "/")
        base_url = url_for('static', filename=rel_filename)
        return f"{base_url}?t={int(time.time())}"
    except Exception:
        name = os.path.basename(fpath)
        return f"/static/{name}?t={int(time.time())}"


app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "uploads")
app.config["GENERATED_FOLDER"] = os.path.join(app.root_path, "static", "generated")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["GENERATED_FOLDER"], exist_ok=True)

JOBS: Dict[str, Dict[str, Any]] = {}

# ============== Chaves & clientes ==============
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
NANO_KEY = (os.getenv("NANO_BANANA_API_KEY") or "").strip()  # Google AI Studio/Vertex

# OpenAI (opcional)
openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"[WARN] Falha ao inicializar OpenAI: {e}")

# Google “genai” (SDK novo) — Imagen 4
genai_client = None
try:
    if NANO_KEY:
        from google import genai as google_genai  # type: ignore
        genai_client = google_genai.Client(api_key=NANO_KEY)
except Exception as e:
    print(f"[WARN] Falha ao inicializar google.genai.Client: {e}")
    genai_client = None

# Google “generativeai” (SDK clássico) — Gemini (texto-imagem pode não retornar bytes inline)
gemini = None
try:
    if NANO_KEY:
        import google.generativeai as generativeai  # type: ignore
        generativeai.configure(api_key=NANO_KEY)
        gemini = generativeai
except Exception as e:
    print(f"[WARN] Falha ao inicializar google.generativeai: {e}")
    gemini = None

# ============== Helpers utilitários ==============

# Limpeza de caracteres binários/controle
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

def _clean_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\u0000", "")
    s = _CONTROL_RE.sub("", s)
    return s.strip()

def _read_txt(file_path: str) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            with open(file_path, "r", encoding=enc, errors="strict") as f:
                return f.read()
        except Exception:
            continue
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

def _read_docx(file_path: str) -> str:
    try:
        from docx import Document  # python-docx
        doc = Document(file_path)
        paras = []
        for p in doc.paragraphs:
            txt = (p.text or "").strip()
            if txt:
                paras.append(txt)
        return "\n\n".join(paras)
    except Exception as e:
        return f"[ERRO ao ler DOCX: {e}]"

def _read_pdf(file_path: str) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        chunks = []
        for page in reader.pages:
            txt = (page.extract_text() or "").strip()
            if txt:
                chunks.append(txt)
        return "\n\n".join(chunks)
    except Exception as e:
        return f"[ERRO ao ler PDF: {e}]"

def safe_read_text(file_path: str) -> str:
    """
    Lê TXT, DOCX e PDF corretamente, higieniza e retorna texto limpo.
    Também tenta heurística por assinatura quando a extensão está incorreta.
    """
    low = file_path.lower()

    # 1) Por extensão
    if low.endswith(".docx"):
        return _clean_text(_read_docx(file_path))
    if low.endswith(".pdf"):
        return _clean_text(_read_pdf(file_path))
    if low.endswith(".txt"):
        return _clean_text(_read_txt(file_path))

    # 2) Heurística por assinatura
    try:
        with open(file_path, "rb") as f:
            head = f.read(8)
    except Exception:
        return _clean_text(_read_txt(file_path))

    if head.startswith(b"PK"):  # DOCX é um ZIP
        return _clean_text(_read_docx(file_path))
    if head.startswith(b"%PDF"):
        return _clean_text(_read_pdf(file_path))
    return _clean_text(_read_txt(file_path))

def split_paragraphs(text: str, max_paragraphs: int = 40) -> List[str]:
    """
    1) Tenta quebras duplas; 2) depois linhas simples; 3) mescla blocos curtos.
    """
    text = _clean_text(text)
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    if not blocks:
        blocks = [b.strip() for b in text.split("\n") if b.strip()]

    merged = []
    buf = ""
    for b in blocks:
        if len(b) < 40:
            buf = (buf + " " + b).strip()
        else:
            if buf:
                merged.append(buf)
                buf = ""
            merged.append(b)
    if buf:
        merged.append(buf)

    return merged[:max_paragraphs]

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

#
# --- FUNÇÃO DE LOG ATUALIZADA ---
#
def log(job_id: str, msg: str):
    # 1. Imprime no terminal para podermos ver o erro
    try:
        # Usamos :6 para pegar só os 6 primeiros caracteres do job_id
        print(f"[JOB: {job_id[:6]}] {msg}")
    except Exception:
        pass # Evita falhar se a impressão der erro
    
    # 2. Adiciona ao log da página web
    if job_id in JOBS:
        if "log" not in JOBS[job_id]:
            JOBS[job_id]["log"] = []
        JOBS[job_id]["log"].append(msg)

def realism_prefix() -> str:
    return ("Foto realista, iluminação natural, lente 35mm, profundidade de campo, "
            "texturas fiéis, balanço de branco neutro, nitidez, sem textos ou logos, qualidade alta.")

def guess_mime(fname: str) -> str:
    low = fname.lower()
    if low.endswith(".png"): return "image/png"
    if low.endswith(".jpg") or low.endswith(".jpeg"): return "image/jpeg"
    if low.endswith(".webp"): return "image/webp"
    return "application/octet-stream"

def load_ref_images_as_parts(paths: List[str]) -> List[Dict[str, Any]]:
    parts = []
    for p in paths:
        try:
            if not p: continue
            with open(p, "rb") as f:
                b = f.read()
            parts.append({"mime_type": guess_mime(p), "data": b})
        except Exception:
            continue
    return parts

def get_img_size(path: str):
    """Retorna (w, h) em pixels ou (None, None) se não conseguir abrir."""
    try:
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None

def parse_wh(sz: str):
    try:
        w, h = [int(x) for x in sz.lower().split("x")]
        return max(1, w), max(1, h)
    except Exception:
        return 1024, 1024

#
# --- FUNÇÃO ensure_image_size ATUALIZADA (com corte/crop) ---
#
def ensure_image_size(fpath: str, target_size: str, log_fn):
    """
    Garante que a imagem salva tenha exatamente o tamanho pedido (target_size).
    Aplica crop e/ou resize para evitar distorção.
    """
    try:
        req_w, req_h = parse_wh(target_size)
        
        with Image.open(fpath) as im:
            im = im.convert("RGB") if im.mode in ("P", "RGBA", "LA") else im
            w, h = im.size
            
            if (w, h) == (req_w, req_h):
                log_fn(f"Tamanho já é o solicitado ({req_w}x{req_h})")
                return

            # 1. Calcular a proporção (aspect ratio) desejada
            req_ratio = req_w / req_h
            img_ratio = w / h
            
            # log_fn(f"[DEBUG] Original: {w}x{h}, Solicitado: {req_w}x{req_h}")

            # 2. Determinar a área de corte (crop)
            if img_ratio > req_ratio:
                # Imagem original é mais larga (landscape) que o necessário.
                # Redimensiona pela altura e depois corta na largura.
                new_h = req_h
                new_w = int(req_ratio * new_h)
                
                # Para evitar distorção, redimensionamos o maior lado para que o menor lado 
                # atinja o mínimo necessário para o crop sem perder pixels cruciais.
                scale_factor = req_h / h
                resize_w = int(w * scale_factor)
                resize_h = req_h
                
                # Redimensiona para o mínimo que permita o crop
                im = im.resize((resize_w, resize_h), Image.LANCZOS)
                
                # Calcula a área de corte (centralizado)
                left = (resize_w - req_w) // 2
                top = 0
                right = left + req_w
                bottom = req_h
                
                im = im.crop((left, top, right, bottom))
                log_fn(f"Aplicado Crop central (Proporção) de {w}x{h} para {req_w}x{req_h}")
                
            elif img_ratio < req_ratio:
                # Imagem original é mais alta (portrait) que o necessário.
                # Redimensiona pela largura e depois corta na altura.
                new_w = req_w
                new_h = int(req_w / req_ratio)
                
                # Redimensiona para o mínimo que permita o crop
                scale_factor = req_w / w
                resize_w = req_w
                resize_h = int(h * scale_factor)
                
                im = im.resize((resize_w, resize_h), Image.LANCZOS)
                
                # Calcula a área de corte (centralizado)
                left = 0
                top = (resize_h - req_h) // 2
                right = req_w
                bottom = top + req_h
                
                im = im.crop((left, top, right, bottom))
                log_fn(f"Aplicado Crop central (Proporção) de {w}x{h} para {req_w}x{req_h}")
                
            else:
                # Proporção é a mesma, apenas redimensiona
                im = im.resize((req_w, req_h), Image.LANCZOS)
                log_fn(f"Apenas redimensionado de {w}x{h} para {req_w}x{req_h} (mesma proporção)")

            # 3. Salva a imagem final no caminho original
            im.save(fpath)
            
    except Exception as e:
        log_fn(f"[ERRO PIL] Falha ao redimensionar/cortar imagem para {target_size}: {e}")
        pass # Ignora e segue em frente (a imagem no tamanho da API ainda estará lá)


# ============== Tamanhos por provedor ==============
# O DALL-E 3 aceita apenas estes:
OPENAI_ALLOWED = {"1024x1024", "1024x1792", "1792x1024"} 
# O Imagen 4 (via genai) aceita estes (para 'imagen-4.0-generate-001'):
NANO_ALLOWED   = {"1024x1024", "1024x1536", "1536x1024"} 

def normalize_size(provider: str, req: str) -> str:
    """
    Retorna o tamanho da API permitido que mais se assemelha à proporção pedida.
    """
    r = (req or "").strip().lower()
    try:
        req_w, req_h = parse_wh(r)
        req_ratio = req_w / req_h
    except Exception:
        # Se a string não for válida, retorna o padrão 1:1
        return "1024x1024"

    best_size = "1024x1024"
    min_diff = float('inf')
    
    allowed = OPENAI_ALLOWED if provider == "openai" else NANO_ALLOWED
    
    for allowed_sz in allowed:
        try:
            w, h = parse_wh(allowed_sz)
            allowed_ratio = w / h
            # Diferença absoluta de proporção
            diff = abs(req_ratio - allowed_ratio)
            
            # Se a diferença for zero, é o melhor match.
            if diff < min_diff:
                min_diff = diff
                best_size = allowed_sz
        except Exception:
            continue # Ignora tamanhos mal-formatados

    return best_size


# ============== Imagen (google.genai) helpers ==============
# Mantemos apenas um modelo válido para evitar 404 de modelos deprecados.
IMAGEN_MODELS = ["imagen-4.0-generate-001"]

def size_to_imagen_dims(sz: str):
    try:
        from google.genai import types as gtypes  # type: ignore
        w, h = [int(x) for x in sz.lower().split("x")]
        return gtypes.ImageDimensions(width=w, height=h)
    except Exception:
        return None

def _save_png_bytes(raw: bytes, out_path: str) -> bool:
    try:
        with open(out_path, "wb") as f:
            f.write(raw)
        return True
    except Exception:
        return False

def _decode_gemini_image_part(part: Any) -> Optional[bytes]:
    try:
        if hasattr(part, "inline_data") and part.inline_data:
            raw = getattr(part.inline_data, "data", None)
            if isinstance(raw, (bytes, bytearray)): return bytes(raw)
            if isinstance(raw, str): return base64.b64decode(raw)
    except Exception:
        pass
    return None

def _extract_imagen_bytes(img_obj: Any) -> Optional[bytes]:
    """
    Tenta extrair bytes de diferentes formas para lidar com mudanças do SDK:
    - obj.image_bytes / obj.bytes / obj.data / obj.base64_data
    - obj.image.image_bytes / obj.image.bytes / obj.image.data / obj.image.base64_data
    """
    try:
        # nível 1
        for attr in ("image_bytes", "bytes", "data", "base64_data"):
            val = getattr(img_obj, attr, None)
            if isinstance(val, (bytes, bytearray)):
                return bytes(val)
            if isinstance(val, str):
                try:
                    return base64.b64decode(val)
                except Exception:
                    pass

        # nível 2 (aninhado em .image)
        inner = getattr(img_obj, "image", None)
        if inner is not None:
            for attr in ("image_bytes", "bytes", "data", "base64_data"):
                val = getattr(inner, attr, None)
                if isinstance(val, (bytes, bytearray)):
                    return bytes(val)
                if isinstance(val, str):
                    try:
                        return base64.b64decode(val)
                    except Exception:
                        pass
    except Exception:
        return None
    return None

# ============== Provedores ==============
def generate_with_imagen(job_id: str, prompt: str, size_api: str, out_dir: str,
                         seed: Optional[int], refs: List[str]) -> Optional[str]:
    if not genai_client:
        log(job_id, "[Imagen] Cliente não inicializado.")
        return None
    try:
        from google.genai import types as gtypes  # type: ignore
    except Exception:
        log(job_id, "[Imagen] types indisponível.")
        return None

    # Usa size_api (tamanho normalizado da API)
    dims = size_to_imagen_dims(size_api)
    full_prompt = f"{realism_prefix()} {prompt}"
    if refs:
        full_prompt += " Replique o estilo/realismo/paleta/iluminação/composição das imagens de referência fornecidas."

    for model in IMAGEN_MODELS:
        try:
            cfg = {"number_of_images": 1}
            if dims: cfg["image_dimensions"] = dims
            #
            # --- CORREÇÃO AQUI ---
            # O SDK do Imagen 4 não aceita 'seed' neste formato.
            # if seed is not None: cfg["seed"] = int(seed)
            #
            
            resp = genai_client.models.generate_images(
                model=model, prompt=full_prompt,
                config=gtypes.GenerateImagesConfig(**cfg)
            ) # <-- PARÊNTESE ADICIONADO AQUI

            # Alguns SDKs retornam 'generated_images', outros 'images'
            imgs = getattr(resp, "generated_images", None) or getattr(resp, "images", None) or []
            if not imgs:
                log(job_id, f"[Imagen:{model}] resposta vazia."); continue

            gi = imgs[0]
            out_name = f"img_{uuid.uuid4().hex[:8]}.png"
            out_path = os.path.join(out_dir, out_name)

            # Tenta extrair bytes de forma robusta
            raw = _extract_imagen_bytes(gi)
            if raw and _save_png_bytes(raw, out_path):
                # A função ensure_image_size será chamada mais tarde com o tamanho do usuário
                # log(job_id, f"[Imagen:{model}] OK (bytes)")
                return out_path

            # Sem bytes utilizáveis
            log(job_id, f"[Imagen:{model}] sem dados de imagem utilizáveis.")
        except Exception as e:
            log(job_id, f"[Imagen:{model}] erro: {e}")
            continue
    return None

def generate_with_gemini(job_id: str, prompt: str, size_api: str, out_dir: str,
                         seed: Optional[int], refs: List[str]) -> Optional[str]:
    """
    Tenta Gemini (google.generativeai). Muitos modelos retornam URI ou partes não-binárias;
    aqui só seguimos se conseguirmos bytes inline. Caso contrário, loga e prossegue.
    """
    if not gemini:
        log(job_id, "[Gemini] Cliente não inicializado.")
        return None

    # Usa size_api (tamanho normalizado da API)
    if size_api == "1536x1024": ratio = "landscape 3:2"
    elif size_api == "1024x1536": ratio = "portrait 2:3"
    else: ratio = "square 1:1"

    full_text = (f"{realism_prefix()} Capture com aparência de fotografia profissional. "
                 f"Aspect ratio desejado: {ratio}. Tema: {prompt}. "
                 f"Replique o estilo/realismo/paleta/iluminação/composição das imagens de referência.")
    parts = load_ref_images_as_parts(refs)
    parts.append(full_text)

    # Modelos 'flash-image' variam por região/versão; se não houver bytes inline, seguimos adiante.
    candidate_models = ["gemini-2.5-flash-image", "gemini-2.0-flash-image", "gemini-2.0-flash-exp-image"]

    for model in candidate_models:
        try:
            m = gemini.GenerativeModel(model)
            gen_cfg = {"temperature": 0.2}
            #
            # --- CORREÇÃO AQUI ---
            # O SDK do Gemini não aceita 'seed' neste formato.
            # if seed is not None: gen_cfg["seed"] = int(seed)
            #
            resp = m.generate_content(parts, generation_config=gen_cfg)

            if not resp or not getattr(resp, "candidates", None):
                log(job_id, f"[Gemini:{model}] resposta vazia."); continue

            img_bytes = None
            for cand in resp.candidates:
                cparts = getattr(cand, "content", None)
                cparts = getattr(cparts, "parts", []) if cparts else []
                for p in cparts:
                    img_bytes = _decode_gemini_image_part(p)
                    if img_bytes: break
                if img_bytes: break

            if not img_bytes:
                log(job_id, f"[Gemini:{model}] não trouxe bytes de imagem."); continue

            ensure_dir(out_dir)
            fpath = os.path.join(out_dir, f"img_{uuid.uuid4().hex[:8]}.png")
            with open(fpath, "wb") as f: f.write(img_bytes)
            # A função ensure_image_size será chamada mais tarde com o tamanho do usuário
            return fpath
        except Exception as e:
            log(job_id, f"[Gemini:{model}] erro: {e}")
            continue
    return None

def generate_with_nano(job_id: str, prompt: str, size_api: str, out_dir: str,
                       seed: Optional[int], refs: List[str]) -> Optional[str]:
    # Ordem segura: 1) Gemini (se bytes inline) → 2) Imagen 4
    path = generate_with_gemini(job_id, prompt, size_api, out_dir, seed, refs)
    if path: return path
    path = generate_with_imagen(job_id, prompt, size_api, out_dir, seed, refs)
    if path: return path
    return None





def generate_with_openai(job_id: str, prompt: str, size_api: str, out_dir: str,
                         refs: List[str]) -> Optional[str]:
    if not openai_client:
        log(job_id, "OpenAI indisponível (sem chave ou falha de inicialização).")
        return None
    try:
        # Usa size_api (tamanho normalizado da API)
        ref_hint = ""
        if refs:
            ref_hint = (" Imite rigorosamente o estilo/realismo/paleta/iluminação das imagens de referência fornecidas. "
                        "Observação: referências não são anexadas à API deste provedor; servem como instrução textual.")
            log(job_id, "Aviso: em OpenAI as referências são apenas orientação textual; a API não recebe imagens.")

        full_prompt = f"{realism_prefix()} {prompt}.{ref_hint}"
        log(job_id, f"[DEBUG] Chamando OpenAI com prompt: {full_prompt} | tamanho: {size_api}")

        res = openai_client.images.generate(
            model="dall-e-3", prompt=full_prompt, size=size_api,
            quality="standard", n=1
        )

        log(job_id, f"[DEBUG] Resposta OpenAI: {res}")

        # Tentar obter a imagem em base64 ou via URL
        img_bytes = None
        image_data = res.data[0] if res.data else None
        if image_data:
            if getattr(image_data, "b64_json", None):
                img_bytes = base64.b64decode(image_data.b64_json)
            elif getattr(image_data, "url", None):
                log(job_id, f"Baixando imagem da URL: {image_data.url}")
                r = requests.get(image_data.url)
                if r.status_code == 200:
                    img_bytes = r.content
                else:
                    log(job_id, f"[ERRO] Falha ao baixar imagem da URL ({r.status_code})")

        if not img_bytes:
            log(job_id, "[ERRO] OpenAI retornou resposta sem imagem utilizável.")
            return None

        ensure_dir(out_dir)
        fpath = os.path.join(out_dir, f"img_{uuid.uuid4().hex[:8]}.png")
        with open(fpath, "wb") as f:
            f.write(img_bytes)
        # A função ensure_image_size será chamada mais tarde com o tamanho do usuário
        log(job_id, f"Imagem OpenAI gerada com sucesso: {fpath} (no tamanho da API: {size_api})")
        return fpath

    except Exception as e:
        import traceback
        log(job_id, f"[ERRO GRAVE OpenAI] {e}\n{traceback.format_exc()}")
        return None



def generate_image(job_id: str, provider: str, prompt: str, size_api: str,
                   out_dir: str, seed: Optional[int], refs: List[str]) -> Optional[str]:
    use_openai_fallback = JOBS.get(job_id, {}).get("use_openai_fallback", False)

    if provider == "nano":
        path = generate_with_nano(job_id, prompt, size_api, out_dir, seed, refs)
        if path: return path
        if use_openai_fallback:
            log(job_id, "Nano falhou/indisponível — tentando OpenAI (fallback)…")
            return generate_with_openai(job_id, prompt, size_api, out_dir, refs)
        else:
            log(job_id, "[AVISO] Fallback OpenAI desativado. Pulando…")
            return None
            
    else:
        path = generate_with_openai(job_id, prompt, size_api, out_dir, refs)
        if path: return path
        log(job_id, "OpenAI falhou/indisponível — tentando Nano (fallback)…")
        return generate_with_nano(job_id, prompt, size_api, out_dir, seed, refs)

# ============== Worker principal ATUALIZADO ==============
def run_job(job_id: str, text_path: str, provider: str, size_api: str, size_user: str,
            seed: Optional[int], ref_paths: List[str], max_count: int):
    try:
        log(job_id, f"Provedor: {provider}")
        log(job_id, f"Tamanho solicitado pelo usuário: {size_user}")
        log(job_id, f"Tamanho da API (normalizado): {size_api}")
        if ref_paths:
            log(job_id, f"Referências recebidas: {len([p for p in ref_paths if p])} (usadas para guiar estilo/tamanho/cores)")
        else:
            log(job_id, "Sem imagens de referência (geração guiada apenas por texto).")

        JOBS[job_id]["status"] = "lendo texto"
        text = safe_read_text(text_path)
        log(job_id, f"Fonte de texto lida — {len(text)} caracteres após limpeza.")
        paragraphs = split_paragraphs(text, max_paragraphs=max_count)
        JOBS[job_id]["total"] = len(paragraphs)
        log(job_id, f"Parágrafos: {len(paragraphs)} (limite: {max_count})")

        out_dir = os.path.join(app.config["GENERATED_FOLDER"], job_id)
        ensure_dir(out_dir)

        JOBS[job_id]["status"] = "gerando imagens"
        pages = []

        for i, para in enumerate(paragraphs, start=1):
            # cancelamento gentil
            if JOBS[job_id].get("cancel", False):
                JOBS[job_id]["status"] = "cancelado"
                log(job_id, "Processamento cancelado pelo usuário.")
                break

            JOBS[job_id]["current"] = i
            titulo = f"Parágrafo {i}"
            log(job_id, f"({i}/{len(paragraphs)}) Gerando para: {para[:90]}…")

            # gera imagem para o parágrafo atual
            # Usamos size_api na geração
            fpath = generate_image(job_id, provider, para, size_api, out_dir, seed, ref_paths)

            imgs: List[str] = []
            img_meta: List[Dict[str, Any]] = []  # metadados por imagem (src, w, h)

            if fpath and os.path.exists(fpath):
                # Pós-processamento para o tamanho exato do usuário
                ensure_image_size(fpath, size_user, lambda msg: log(job_id, f"[PÓS-PROC] {msg}"))

                rel = fpath.replace(app.root_path, "").replace("\\", "/")
                if not rel.startswith("/"):
                    rel = "/" + rel
                w, h = get_img_size(fpath)
                imgs.append(rel)
                img_meta.append({"src": rel, "w": w, "h": h})
                if w and h:
                    log(job_id, f"→ imagem salva e ajustada: {rel} ({w}x{h})")
                else:
                    log(job_id, f"→ imagem salva: {rel}")
            else:
                log(job_id, "[AVISO] Não foi possível gerar imagem para este parágrafo.")

            pages.append({
                "title": titulo,
                "paragraph": para,  # usado no preview do prompt
                "provider": provider,
                "model": "auto",
                "images": imgs,          # mantido por compatibilidade
                "image_meta": img_meta,  # usado pelo progress.html para mostrar px × px
            })
            JOBS[job_id]["pages"] = pages

        if JOBS[job_id].get("status") != "cancelado":
            JOBS[job_id]["status"] = "concluido"
            log(job_id, "Processamento concluído.")
    except Exception as e:
        JOBS[job_id]["status"] = "erro"
        log(job_id, f"Erro: {e}")

# ============== Rotas ==============
@app.get("/")
def index():
    return render_template("index.html")

# ============== Rota /start_upload ATUALIZADA ==============
@app.post("/start_upload")
def start_upload():
    up = request.files.get("file")
    pasted_text = (request.form.get("paste" \
    "d_text") or "").strip()

    provider = request.form.get("provider", "nano")  # "nano" | "openai"
    size_custom = (request.form.get("size_custom") or "").strip()
    size_req = (size_custom or request.form.get("size", "1536x1024")) # <-- TAMANHO REQUISITADO PELO USUÁRIO
    seed_str = (request.form.get("seed", "") or "").strip()

    # quantidade (limite de parágrafos)
    str_count = (request.form.get("count") or "").strip()
    try:
        max_count = max(1, min(200, int(str_count)))
    except Exception:
        max_count = 40

    # fallback OpenAI (checkbox) — DESMARCADO por padrão
    use_openai_fallback = bool(request.form.get("fallback_openai"))

    seed = int(seed_str) if seed_str.isdigit() else None

    # imagens de referência (até 3)
    ref_files = [request.files.get("ref1"), request.files.get("ref2"), request.files.get("ref3")]
    ref_paths: List[str] = []
    for rf in ref_files:
        if rf and rf.filename:
            from werkzeug.utils import secure_filename
            fname = secure_filename(rf.filename)
            local = os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex}_{fname}")
            rf.save(local)
            ref_paths.append(local)

    # origem do texto: upload OU colado
    src_path: Optional[str] = None
    if up and up.filename:
        from werkzeug.utils import secure_filename
        filename = secure_filename(up.filename)
        src_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex}_{filename}")
        up.save(src_path)
    elif pasted_text:
        filename = f"pasted_{uuid.uuid4().hex}.txt"
        src_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(pasted_text)
    else:
        # Se não houver arquivo nem texto, volte para o index.
        # Você pode querer adicionar uma mensagem de erro aqui.
        return redirect(url_for("index"))

    # O tamanho real que será usado na chamada da API
    size_api = normalize_size(provider, size_req) 

    job_id = uuid.uuid4().hex
    now_ts = time.time()

    JOBS[job_id] = {
        "status": "iniciando",
        "total": 0,
        "current": 0,
        "log": ["Execução iniciada."],
        "pages": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "started_ts": now_ts,
        "use_openai_fallback": use_openai_fallback,
        "cancel": False,
        "original_size": size_api,      # <-- TAMAHO DA API
        "user_requested_size": size_req, # <-- TAMANHO DO USUÁRIO (NOVO)
        "ref_paths": ref_paths,     # <-- Salva caminhos das refs
    }

    log(job_id, f"Tamanho solicitado: {size_req} | Tamanho API: {size_api} ({provider})")

    t = threading.Thread(
        target=run_job,
        args=(job_id, src_path, provider, size_api, size_req, seed, ref_paths, max_count), # <-- size_user (size_req) adicionado
        daemon=True
    )
    t.start()

    return redirect(url_for("progress", job_id=job_id))

# -------- Cancelar job --------
@app.post("/cancel/<job_id>")
def cancel(job_id):
    if job_id in JOBS:
        JOBS[job_id]["cancel"] = True
        JOBS[job_id]["status"] = "cancelando"
        log(job_id, "Solicitação de cancelamento recebida.")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "job_id desconhecido"}), 404

# -------- Ping de Provedores --------
@app.get("/ping")
def ping():
    report = {
        "openai": {"configured": bool(OPENAI_API_KEY), "ok": False, "detail": ""},
        "gemini": {"configured": bool(NANO_KEY), "ok": False, "detail": ""},
        "imagen": {"configured": bool(NANO_KEY), "ok": False, "detail": ""},
    }

    if OPENAI_API_KEY and openai_client:
        try:
            _ = openai_client.models.list()
            report["openai"]["ok"] = True
            report["openai"]["detail"] = "Client OK (models.list)"
        except Exception as e:
            report["openai"]["detail"] = f"erro: {e}"

    if NANO_KEY and gemini:
        try:
            models = gemini.list_models()
            names = [getattr(m, "name", "") for m in models]
            has_flash_image = any("flash-image" in n for n in names)
            report["gemini"]["ok"] = True if names else False
            report["gemini"]["detail"] = "flash-image disponível" if has_flash_image else "client OK (list_models)"
        except Exception as e:
            report["gemini"]["detail"] = f"erro: {e}"

    if NANO_KEY and genai_client:
        try:
            _ = genai_client.models.list()
            report["imagen"]["ok"] = True
            report["imagen"]["detail"] = "client OK (models.list)"
        except Exception as e:
            report["imagen"]["detail"] = f"erro: {e}"

    return jsonify(report)

# -------- Progresso / Status --------
@app.get("/progress/<job_id>")
def progress(job_id):
    # Esta linha procura 'progress.html' dentro da pasta 'templates'
    return render_template("progress.html", job_id=job_id)

@app.get("/status/<job_id>")
def status(job_id):
    data = JOBS.get(job_id)
    if not data:
        return jsonify({"status": "erro", "message": "job_id desconhecido"}), 404

    # métricas de tempo
    elapsed = 0.0
    eta = None
    if data.get("started_ts"):
        elapsed = max(0.0, time.time() - float(data["started_ts"]))
        cur = int(data.get("current", 0))
        tot = int(data.get("total", 0))
        if cur > 0 and tot > 0 and data.get("status") == "gerando imagens":
            avg = elapsed / cur
            remaining = max(0, tot - cur)
            eta = remaining * avg

    return jsonify({
        "status": data["status"],
        "total": data["total"],
        "current": data["current"],
        "log": data["log"],
        "pages": data["pages"],
        "elapsed_sec": elapsed,
        "eta_sec": eta,
        "cancel": data.get("cancel", False),
    })

@app.get("/status/")
def status_missing():
    return jsonify({"status": "erro", "message": "job_id ausente na URL"}), 400

@app.get("/uploads/<path:fname>")
def serve_upload(fname):
    return send_from_directory(app.config["UPLOAD_FOLDER"], fname)

#
# --- ROTA /regenerate ATUALIZADA (CORRIGINDO O CRASH E FORÇANDO VARIAÇÃO) ---
#

@app.route('/results')
def results():
    """
    Renderiza a página de resultados da galeria,
    passando o job_id da query string para o template (se necessário).
    """
    job_id = request.args.get('job_id')
    
    # Esta rota apenas renderiza o template results.html. 
    # A galeria é carregada via JavaScript/fetch no lado do cliente.
    return render_template('results.html', job_id=job_id)

# Nota: A rota '/regenerated_json' também é usada pelo results.html
# Certifique-se de que a rota '/regenerated_json' esteja definida corretamente
# para retornar a lista de imagens para o job_id.


@app.route("/regenerate", methods=["POST"])
def regenerate():
    job_id = request.args.get("job_id")
    p_idx_str = request.args.get("paragraph_idx")
    
    if not job_id or not p_idx_str:
        return jsonify({"error": "job_id ou paragraph_idx ausente"}), 400
    
    job_data = JOBS.get(job_id)
    if not job_data:
        return jsonify({"error": "job não encontrado"}), 404
        
    try:
        p_idx = int(p_idx_str)
        page_data = job_data.get("pages", [])[p_idx]
        original_prompt = page_data["paragraph"]
    except Exception as e:
        log(job_id, f"[ERRO] Índice de parágrafo inválido: {e}")
        return jsonify({"error": "parágrafo inválido"}), 400

    try:
        data = request.json or {}
        edit_prompt = (data.get("edit") or "").strip()
        req_size = (data.get("size") or "").strip() # Tamanho solicitado pelo usuário
        free_variation = bool(data.get("free"))
        original_src_url = (data.get("original_src") or "").strip() 

        # --- CORREÇÃO AQUI: Força variação adicionando texto ao prompt ---
        variation_id = random.randint(1000, 9999) # Gera um número aleatório
        
        # Constrói o novo prompt
        new_prompt = original_prompt
        
        if edit_prompt and not free_variation:
             # Adiciona ao prompt de edição
             new_prompt = f"{original_prompt}. Instrução de edição: {edit_prompt}. (Variação: {variation_id})"
        elif free_variation:
             # Adiciona ao prompt de variação livre
             new_prompt = f"{original_prompt}. (Gerar uma variação livre com estilo similar) (Variação: {variation_id})"
        else:
             # Se o usuário não digitou nada, força uma variação
             new_prompt = f"{original_prompt} (Variação aleatória: {variation_id})"

        
        # Busca dados originais do job
        provider = page_data.get("provider", "nano")
        
        # Pegamos o tamanho original (user_requested_size)
        user_size = job_data.get("user_requested_size", "1024x1024") 
        final_user_size = req_size or user_size
        
        # O tamanho da API é baseado no tamanho final do usuário
        size_api = normalize_size(provider, final_user_size)
        
        # --- INÍCIO DA LÓGICA DE EDIÇÃO ---
        
        # Pega as referências ORIGINAIS do job (se houver) e garante que é uma lista
        ref_paths = list(job_data.get("ref_paths", [])) 
        
        # Converte o URL da imagem a ser editada em um caminho de arquivo seguro
        # Isso só funciona bem com o 'nano' (Gemini/Imagen)
        if original_src_url and provider == "nano":
            try:
                static_root = app.static_folder if hasattr(app, "static_folder") and app.static_folder else os.path.join(app.root_path, "static")

                # --- INÍCIO DA CORREÇÃO ---
                # Usa urlparse para lidar com URLs completos (http://...) ou caminhos relativos (/static/...)
                parsed_url = urlparse(original_src_url)
                src_path = parsed_url.path # Extrai o caminho, ex: /static/generated/job123/img.png
                
                # Limpa query strings (urlparse já pode ter feito, mas é uma garantia)
                src_path = src_path.split("?")[0]

                # Remove o prefixo /static/ para obter o caminho relativo ao 'static_root'
                if src_path.startswith("/static/"):
                    rel = src_path[len("/static/"):]
                else:
                    rel = src_path.lstrip("/")
                    
                # Segurança: impede subida de diretório (path traversal)
                rel = rel.replace("..", "").lstrip("/")
                
                fpath_to_edit = os.path.join(static_root, rel)
                # --- FIM DA CORREÇÃO ---
                
                if os.path.isfile(fpath_to_edit):
                    # Adiciona a imagem a ser editada no INÍCIO da lista de referências
                    # A IA vai usá-la como base principal
                    ref_paths.insert(0, fpath_to_edit) 
                    log(job_id, f"Usando imagem existente como referência forte para edição: {fpath_to_edit}")
                else:
                    log(job_id, f"[AVISO] Não foi possível encontrar a imagem de referência local: {fpath_to_edit} (URL original: {original_src_url})")
            except Exception as e:
                log(job_id, f"[AVISO] Falha ao processar caminho da imagem de referência: {e}")
        
        # --- FIM DA LÓGICA DE EDIÇÃO ---
        
        out_dir = os.path.join(app.config["GENERATED_FOLDER"], job_id)
        
        # O 'seed' que causou o crash não é mais passado para a função de geração
        seed = None 
        
        log(job_id, f"Regenerando parágrafo {p_idx+1} (Refs: {len(ref_paths)}) (Prompt: {new_prompt[:90]}...)")
        log(job_id, f"[DEBUG] Provider: {provider}, Size API: {size_api}, Size User: {final_user_size}")

        # Chamada à função de geração de imagem (usamos size_api)
        try:
            fpath = generate_image(job_id, provider, new_prompt, size_api, out_dir, seed, ref_paths)
            
            if fpath and os.path.exists(fpath):
                # PÓS-PROCESSAMENTO PARA O TAMANHO FINAL AQUI
                ensure_image_size(fpath, final_user_size, lambda msg: log(job_id, f"[PÓS-PROC] {msg}"))
                log(job_id, f"[DEBUG] Geração bem-sucedida, arquivo gerado: {fpath}")
            else:
                log(job_id, f"[DEBUG] generate_image retornou None, verifique se a OpenAI retornou erro")
        except Exception as e:
            import traceback
            log(job_id, f"[ERRO] Falha ao gerar imagem: {e}\n{traceback.format_exc()}")
            return jsonify({"error": f"Falha ao gerar imagem: {e}"}), 500

        
        if fpath and os.path.exists(fpath):
            rel = fpath.replace(app.root_path, "").replace("\\", "/")
            if not rel.startswith("/"):
                rel = "/" + rel
            w, h = get_img_size(fpath)
            log(job_id, f"→ Imagem regenerada e ajustada: {rel} ({w}x{h}px)")
            
            # --- CORREÇÃO DE INDENTAÇÃO AQUI ---
            # Memoriza a nova versão para aparecer imediatamente na UI/galeria
            try:
                page_data.setdefault("image_meta", []).append({"src": rel, "w": w, "h": h, "prompt": new_prompt})
                page_data.setdefault("images", []).append(rel)
                job_data["pages"][p_idx] = page_data
                JOBS[job_id] = job_data
            except Exception as _memerr:
                log(job_id, f"[WARN] Não foi possível memorizar imagem regenerada no estado: {_memerr}")
            
            # Retorna 200 (OK)
            return jsonify({"src": rel, "w": w, "h": h})
        else:
            # Isso acontece se generate_image falhar sem travar
            log(job_id, "[ERRO] Falha ao regenerar imagem (generate_image retornou None).")
            # Retorna 500 (Erro de Servidor)
            return jsonify({"error": "Falha ao gerar imagem"}), 500
            
    except Exception as e:
        # --- Captura qualquer outro crash (Erro 500) ---
        log(job_id, f"[ERRO GRAVE] Crash na rota /regenerate: {e}")
        return jsonify({"error": f"Falha ao gerar imagem: {e}"}), 500

if __name__ == "__main__":
    print("→ Iniciando servidor em http://127.0.0.1:5001")
    print("→ Este app lê bem textos em PT-BR e extrai DOCX/PDF corretamente.")
    print("→ ATENÇÃO: Certifique-se que 'index.html' e 'progress.html' estão na pasta 'templates'")
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)


@app.get("/regenerated_json")
def regenerated_json():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error":"missing job_id"}), 400
    items = []
    job = JOBS.get(job_id) or {}
    pages = job.get("pages", [])
    for pidx, page in enumerate(pages):
        metas = page.get("image_meta") or []
        for i, m in enumerate(metas):
            if not m: continue
            items.append({
                "page_index": pidx,
                "version": i+1,
                "src": m.get("src"),
                "w": m.get("w"),
                "h": m.get("h"),
                "prompt": m.get("prompt")
            })
    if not items:
        static_root = app.static_folder if hasattr(app, "static_folder") and app.static_folder else os.path.join(app.root_path, "static")
        alt_root = os.path.join(app.root_path, "statics")
        candidates = []
        for root in [static_root, alt_root]:
            if not root: continue
            d1 = os.path.join(root, "generated", job_id)
            if os.path.isdir(d1):
                import glob
                candidates.extend(sorted(glob.glob(os.path.join(d1, "*.*"))))
        for idx, fpath in enumerate(candidates, start=1):
            url = static_url_with_buster(fpath)
            items.append({"page_index": 0, "version": idx, "src": url, "w": None, "h": None, "prompt": None})
    return jsonify({"job_id": job_id, "items": items})


@app.get("/regenerated")
def regenerated_page():
    try:
        job_id = (request.args.get("job_id") or "").strip()
    except Exception:
        job_id = ""
    # A página monta a lista via /regenerated_json; manter vazio aqui
    return render_template("regenerated.html", job_id=job_id, items=[])


@app.get("/dl")
def dl():
    """
    Download seguro de arquivos dentro de /static.
    Parâmetros:
      - src: caminho relativo ou absoluto (pode conter ?t=)
      - name: nome sugerido do arquivo (opcional)
    """
    try:
        src = (request.args.get("src") or "").strip()
        name = (request.args.get("name") or "").strip()
        if not src:
            return jsonify({"error":"missing src"}), 400
        # Remove query string e força caminho relativo sob /static
        src = src.split("?")[0]
        # Normaliza para começar com /
        if not src.startswith("/"):
            src = "/" + src
        # Garante que a origem seja a pasta /static do app
        static_root = app.static_folder if hasattr(app, "static_folder") and app.static_folder else os.path.join(app.root_path, "static")
        # remove prefixo '/static/'
        if src.startswith("/static/"):
            rel = src[len("/static/"):]
        else:
            # tenta fazer caminho relativo a partir do root path
            rel = src.lstrip("/")
        # Evitar path traversal
        rel = rel.replace("..", "").lstrip("/")
        fdir = static_root
        fpath = os.path.join(fdir, rel)
        if not os.path.isfile(fpath):
            return jsonify({"error":"file not found"}), 404
        download_name = name if name else os.path.basename(rel)
        try:
            # Flask >= 2.0
            return send_from_directory(fdir, rel, as_attachment=True, download_name=download_name)
        except TypeError:
            # Flask antigo
            return send_from_directory(fdir, rel, as_attachment=True, attachment_filename=download_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500