import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("nerdearla.store")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "subtitles.db")


class SubtitleRecord(BaseModel):
    """Registro individual de una frase finalizada con timestamps relativos."""
    index: int = Field(..., description="Índice secuencial para el subtítulo")
    start_seconds: float = Field(..., description="Segundo de inicio relativo al inicio de la sala")
    end_seconds: float = Field(..., description="Segundo de finalización relativo")
    original_text: str = Field(..., description="Texto en idioma original")
    original_lang: str = Field("en", description="Idioma original detectado o configurado")
    translations: Dict[str, str] = Field(default_factory=dict, description="Traducciones por código de idioma")
    speaker: Optional[str] = Field(None, description="Nombre del disertante identificado")
    speaker_color: Optional[str] = Field(None, description="Color distintivo del disertante")


class SubtitleStore:
    """
    Almacén de subtítulos con persistencia en SQLite y caché en memoria.
    Garantiza que toda la conversación se preserve permanentemente para nuevos espectadores
    y descargas (SRT, VTT, TXT, PDF, HTML) incluso tras reinicios del servidor.
    """

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._rooms_start: Dict[str, float] = {}
        self._rooms_history: Dict[str, List[SubtitleRecord]] = {}
        self._init_db()
        self._load_from_db()

    def _init_db(self) -> None:
        """Inicializa el esquema de SQLite en data/subtitles.db si no existe."""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS room_clocks (
                        room_id TEXT PRIMARY KEY,
                        start_time REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS subtitles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id TEXT NOT NULL,
                        seq_index INTEGER NOT NULL,
                        start_seconds REAL NOT NULL,
                        end_seconds REAL NOT NULL,
                        original_text TEXT NOT NULL,
                        original_lang TEXT NOT NULL,
                        translations_json TEXT NOT NULL,
                        speaker TEXT,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_room_seq ON subtitles(room_id, seq_index)
                """)
                # Migración no destructiva si la columna speaker no existía previamente
                try:
                    conn.execute("ALTER TABLE subtitles ADD COLUMN speaker TEXT")
                except Exception:
                    pass
                conn.commit()

    def _load_from_db(self) -> None:
        """Carga en memoria todos los subtítulos y marcas de tiempo persistidas."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT room_id, start_time FROM room_clocks")
                for r_id, s_time in cursor.fetchall():
                    self._rooms_start[r_id] = s_time

                cursor.execute("""
                    SELECT room_id, seq_index, start_seconds, end_seconds, original_text, original_lang, translations_json, speaker
                    FROM subtitles
                    ORDER BY room_id, seq_index ASC
                """)
                loaded_count = 0
                for r_id, idx, s_sec, e_sec, orig_text, orig_lang, trans_json, spk in cursor.fetchall():
                    try:
                        trans = json.loads(trans_json) if trans_json else {}
                    except Exception:
                        trans = {}
                    rec = SubtitleRecord(
                        index=idx,
                        start_seconds=s_sec,
                        end_seconds=e_sec,
                        original_text=orig_text,
                        original_lang=orig_lang,
                        translations=trans,
                        speaker=spk
                    )
                    self._rooms_history.setdefault(r_id, []).append(rec)
                    loaded_count += 1
                if loaded_count > 0:
                    logger.info("Cargados %d subtítulos históricos persistidos desde SQLite.", loaded_count)

    def start_room_clock(self, room_id: str, reset: bool = False) -> None:
        """Marca o preserva el t=0 para una sala. Si no se pide reset, no borra el historial existente."""
        now = time.time()
        if reset or room_id not in self._rooms_start:
            self._rooms_start[room_id] = now
            if reset:
                self._rooms_history[room_id] = []
                with self._lock:
                    with sqlite3.connect(self._db_path) as conn:
                        conn.execute("DELETE FROM subtitles WHERE room_id = ?", (room_id,))
                        conn.execute("INSERT OR REPLACE INTO room_clocks (room_id, start_time) VALUES (?, ?)", (room_id, now))
                        conn.commit()
            else:
                with self._lock:
                    with sqlite3.connect(self._db_path) as conn:
                        conn.execute("INSERT OR IGNORE INTO room_clocks (room_id, start_time) VALUES (?, ?)", (room_id, now))
                        conn.commit()
        logger.info("Reloj de sala '%s' activo (t0=%.2f, acumulados=%d)", room_id, self._rooms_start[room_id], len(self._rooms_history.get(room_id, [])))

    def add_subtitle(
        self,
        room_id: str,
        original_text: str,
        original_lang: str = "en",
        translations: Optional[Dict[str, str]] = None,
        duration_sec: float = 3.0,
        speaker: Optional[str] = None,
        speaker_color: Optional[str] = None
    ) -> SubtitleRecord:
        """
        Agrega un subtítulo finalizado con identificación de orador opcional,
        lo persiste en SQLite y lo guarda en memoria.
        """
        start_time_wall = self._rooms_start.get(room_id)
        if start_time_wall is None:
            self.start_room_clock(room_id)
            start_time_wall = self._rooms_start[room_id]

        history = self._rooms_history.setdefault(room_id, [])
        end_rel = max(time.time() - start_time_wall, 0.5)
        start_rel = max(end_rel - duration_sec, 0.0)

        record = SubtitleRecord(
            index=len(history) + 1,
            start_seconds=start_rel,
            end_seconds=end_rel,
            original_text=original_text,
            original_lang=original_lang,
            translations=translations or {},
            speaker=speaker,
            speaker_color=speaker_color
        )
        history.append(record)

        # Persistencia en base de datos SQLite
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO subtitles (room_id, seq_index, start_seconds, end_seconds, original_text, original_lang, translations_json, speaker, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            room_id,
                            record.index,
                            record.start_seconds,
                            record.end_seconds,
                            record.original_text,
                            record.original_lang,
                            json.dumps(record.translations, ensure_ascii=False),
                            speaker,
                            time.time()
                        )
                    )
                    conn.commit()
        except Exception as e:
            logger.error("Error guardando subtítulo en SQLite: %s", e)

        return record

    def get_history(self, room_id: str, limit: Optional[int] = None) -> List[SubtitleRecord]:
        """Obtiene la lista de subtítulos emitidos en la sala (todos o con límite)."""
        with self._lock:
            if room_id not in self._rooms_history:
                try:
                    with sqlite3.connect(self._db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT seq_index, start_seconds, end_seconds, original_text, original_lang, translations_json, speaker
                            FROM subtitles
                            WHERE room_id = ?
                            ORDER BY seq_index ASC
                        """, (room_id,))
                        rows = cursor.fetchall()
                        recs = []
                        for idx, s_sec, e_sec, orig_text, orig_lang, trans_json, spk in rows:
                            try:
                                trans = json.loads(trans_json) if trans_json else {}
                            except Exception:
                                trans = {}
                            recs.append(SubtitleRecord(
                                index=idx,
                                start_seconds=s_sec,
                                end_seconds=e_sec,
                                original_text=orig_text,
                                original_lang=orig_lang,
                                translations=trans,
                                speaker=spk
                            ))
                        self._rooms_history[room_id] = recs
                except Exception as e:
                    logger.warning("Error consultando historial en SQLite para '%s': %s", room_id, e)

            history = self._rooms_history.get(room_id, [])
            if limit is not None and limit > 0:
                return list(history[-limit:])
            return list(history)

    @staticmethod
    def _format_timestamp(seconds: float, vtt: bool = False) -> str:
        """Convierte segundos a formato HH:MM:SS,mmm (SRT) o HH:MM:SS.mmm (VTT)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        sep = "." if vtt else ","
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"

    def export_srt(self, room_id: str, lang: str = "es") -> str:
        """Exporta los subtítulos en formato SubRip (.srt) con identificación de orador."""
        history = self.get_history(room_id)
        blocks = []
        for record in history:
            text = record.translations.get(lang, record.original_text)
            spk_prefix = f"[{record.speaker}] " if record.speaker else ""
            start_fmt = self._format_timestamp(record.start_seconds, vtt=False)
            end_fmt = self._format_timestamp(record.end_seconds, vtt=False)
            block = f"{record.index}\n{start_fmt} --> {end_fmt}\n{spk_prefix}{text}\n"
            blocks.append(block)
        return "\n".join(blocks)

    def export_vtt(self, room_id: str, lang: str = "es") -> str:
        """Exporta los subtítulos en formato WebVTT (.vtt) con etiqueta de voz."""
        history = self.get_history(room_id)
        lines = ["WEBVTT\n"]
        for record in history:
            text = record.translations.get(lang, record.original_text)
            voice_tag = f"<v {record.speaker}>{text}</v>" if record.speaker else text
            start_fmt = self._format_timestamp(record.start_seconds, vtt=True)
            end_fmt = self._format_timestamp(record.end_seconds, vtt=True)
            block = f"{record.index}\n{start_fmt} --> {end_fmt}\n{voice_tag}\n"
            lines.append(block)
        return "\n".join(lines)

    def _get_paragraphs(self, room_id: str, lang: str = "es", sentences_per_para: int = 4) -> List[str]:
        """Agrupa frases consecutivas en párrafos cohesivos estructurados por orador."""
        history = self.get_history(room_id)
        if not history:
            return []

        paragraphs: List[str] = []
        current: List[str] = []
        current_speaker: Optional[str] = None

        for record in history:
            t = (record.translations.get(lang) or record.original_text).strip()
            if not t:
                continue

            spk = record.speaker
            if spk != current_speaker and current:
                prefix = f"[{current_speaker}] " if current_speaker else ""
                paragraphs.append(prefix + " ".join(current))
                current = []
                current_speaker = spk
            elif current_speaker is None and spk:
                current_speaker = spk

            current.append(t)
            if (len(current) >= sentences_per_para and t.endswith((".", "?", "!"))) or len(current) >= 6:
                prefix = f"[{current_speaker}] " if current_speaker else ""
                paragraphs.append(prefix + " ".join(current))
                current = []
                current_speaker = spk

        if current:
            prefix = f"[{current_speaker}] " if current_speaker else ""
            paragraphs.append(prefix + " ".join(current))

        return paragraphs

    def export_txt(self, room_id: str, lang: str = "es", room_name: str = "") -> str:
        """
        Exporta la transcripción continua en texto plano formateado, centrado y limpio.
        Ideal para lectura fluida, archivo documental o ingestión directa en modelos de IA.
        """
        history = self.get_history(room_id)
        paragraphs = self._get_paragraphs(room_id, lang)
        title = room_name or room_id

        duration_sec = history[-1].end_seconds if history else 0.0
        dur_min = int(duration_sec // 60)
        dur_sec = int(duration_sec % 60)
        orig_lang = history[0].original_lang.upper() if history else "EN"
        total_words = sum(len(p.split()) for p in paragraphs)

        header = [
            "=" * 78,
            "NEJOYT — TRANSCRIPCIÓN OFICIAL DE CONFERENCIA",
            "Evento: Nerdearla 2026",
            f"Sala: {title}",
            f"Idioma: {lang.upper()} (Idioma original de audio: {orig_lang})",
            f"Duración de exposición: {dur_min}m {dur_sec}s",
            f"Total de oraciones: {len(history)} | Palabras registradas: ~{total_words}",
            "=" * 78,
            "",
            ""
        ]

        if not paragraphs:
            return "\n".join(header) + "No se registraron transcripciones para esta sala aún.\n"

        body = "\n\n".join(paragraphs)
        footer = [
            "",
            "",
            "-" * 78,
            "Documento generado automáticamente por NejoyT Subtitulado Inteligente.",
            "Tecnología: Gemini Live Transcribe & Traducción Simultánea Nerdearla.",
            "-" * 78
        ]
        return "\n".join(header) + body + "\n".join(footer)

    def export_pdf(self, room_id: str, lang: str = "es", room_name: str = "") -> bytes:
        """
        Genera un archivo PDF profesional maquetado en A4, con encabezado de marca,
        diseño editorial centrado, metadatos y cuerpo organizado en párrafos de alta legibilidad.
        """
        from io import BytesIO
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
            from reportlab.pdfgen import canvas

            class NumberedCanvas(canvas.Canvas):
                """Agrega pie de página con paginación dinámica 'Página X de Y'."""
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self._saved_page_states = []

                def showPage(self):
                    self._saved_page_states.append(dict(self.__dict__))
                    self._startPage()

                def save(self):
                    num_pages = len(self._saved_page_states)
                    for state in self._saved_page_states:
                        self.__dict__.update(state)
                        self.draw_page_decorations(num_pages)
                        super().showPage()
                    super().save()

                def draw_page_decorations(self, page_count):
                    self.saveState()
                    self.setFont("Helvetica", 8)
                    self.setFillColor(colors.HexColor("#64748b"))
                    # Header fino
                    self.drawString(54, 800, "NejoyT · Transcripción Nerdearla 2026")
                    self.setStrokeColor(colors.HexColor("#e2e8f0"))
                    self.setLineWidth(0.5)
                    self.line(54, 792, 541, 792)

                    # Footer
                    self.line(54, 45, 541, 45)
                    page_text = f"Página {self._pageNumber} de {page_count}"
                    self.drawRightString(541, 32, page_text)
                    self.drawString(54, 32, "Generado por NejoyT Subtitulado Simultáneo · Conf. Nerdearla 2026")
                    self.restoreState()

            buffer = BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                leftMargin=54,
                rightMargin=54,
                topMargin=58,
                bottomMargin=58
            )

            styles = getSampleStyleSheet()
            
            title_style = ParagraphStyle(
                'DocTitle',
                parent=styles['Normal'],
                fontName='Helvetica-Bold',
                fontSize=20,
                leading=24,
                textColor=colors.HexColor('#0f172a'),
                alignment=TA_CENTER,
                spaceAfter=6
            )
            
            sub_style = ParagraphStyle(
                'DocSub',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=11,
                leading=14,
                textColor=colors.HexColor('#fe323c'),
                alignment=TA_CENTER,
                spaceAfter=14
            )

            meta_style = ParagraphStyle(
                'DocMeta',
                parent=styles['Normal'],
                fontName='Helvetica',
                fontSize=9,
                leading=13,
                textColor=colors.HexColor('#334155'),
                alignment=TA_CENTER
            )

            body_style = ParagraphStyle(
                'DocBody',
                parent=styles['Normal'],
                fontName='Times-Roman',
                fontSize=10.5,
                leading=16,
                textColor=colors.HexColor('#1e293b'),
                alignment=TA_JUSTIFY,
                spaceAfter=11,
                firstLineIndent=14
            )

            empty_style = ParagraphStyle(
                'DocEmpty',
                parent=styles['Normal'],
                fontName='Helvetica-Oblique',
                fontSize=11,
                leading=15,
                textColor=colors.HexColor('#94a3b8'),
                alignment=TA_CENTER,
                spaceAfter=15
            )

            story = []
            title = room_name or room_id

            story.append(Paragraph("NERDEARLA 2026", sub_style))
            story.append(Paragraph(title, title_style))
            story.append(Spacer(1, 4))

            history = self.get_history(room_id)
            paragraphs = self._get_paragraphs(room_id, lang)
            duration_sec = history[-1].end_seconds if history else 0.0
            dur_min = int(duration_sec // 60)
            dur_sec = int(duration_sec % 60)
            orig_lang = history[0].original_lang.upper() if history else "EN"
            total_words = sum(len(p.split()) for p in paragraphs)

            meta_text = (
                f"<b>Idioma:</b> {lang.upper()} &nbsp;|&nbsp; "
                f"<b>Audio Original:</b> {orig_lang} &nbsp;|&nbsp; "
                f"<b>Duración:</b> {dur_min}m {dur_sec}s &nbsp;|&nbsp; "
                f"<b>Palabras:</b> ~{total_words}"
            )
            story.append(Paragraph(meta_text, meta_style))
            story.append(Spacer(1, 10))
            story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#cbd5e1'), spaceBefore=4, spaceAfter=14))

            if not paragraphs:
                story.append(Spacer(1, 30))
                story.append(Paragraph("Esta sala aún no tiene oraciones transcritas registradas.", empty_style))
            else:
                for para in paragraphs:
                    # Escapar caracteres HTML básicos para reportlab
                    escaped_para = para.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    story.append(Paragraph(escaped_para, body_style))

            doc.build(story, canvasmaker=NumberedCanvas)
            pdf_bytes = buffer.getvalue()
            buffer.close()
            return pdf_bytes

        except Exception as e:
            logger.error("Error generando PDF con ReportLab: %s", e)
            # Retornar PDF mínimo de fallback
            return b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R>>endobj\nxref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF"

    def export_html_doc(self, room_id: str, lang: str = "es", room_name: str = "", auto_print: bool = False) -> str:
        """
        Retorna una página web de lectura editorial centrada y lista para imprimir a PDF vía navegador.
        Incluye barra de utilidades para imprimir directo, copiar texto para prompts de IA o descargar TXT.
        """
        history = self.get_history(room_id)
        paragraphs = self._get_paragraphs(room_id, lang)
        title = room_name or room_id

        duration_sec = history[-1].end_seconds if history else 0.0
        dur_min = int(duration_sec // 60)
        dur_sec = int(duration_sec % 60)
        orig_lang = history[0].original_lang.upper() if history else "EN"
        total_words = sum(len(p.split()) for p in paragraphs)
        lang_names = {"es": "Español", "en": "English", "pt": "Português"}
        lang_display = lang_names.get(lang, lang.upper())

        paragraphs_html = "\n".join(f"<p>{p}</p>" for p in paragraphs) if paragraphs else "<p class='empty-note'>No hay oraciones registradas aún en esta ponencia.</p>"
        auto_print_script = "window.addEventListener('load', () => { setTimeout(() => window.print(), 400); });" if auto_print else ""

        raw_clean_text = "\n\n".join(paragraphs)

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Transcripción: {title} — Nerdearla 2026</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #0b0f19;
      --paper: #ffffff;
      --text: #1e293b;
      --text-muted: #64748b;
      --accent: #fe323c;
      --border: #e2e8f0;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: var(--bg);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      color: var(--text);
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
      padding: 2rem 1rem 4rem;
    }}

    /* ACTION BAR FLOTANTE */
    .action-bar {{
      position: sticky;
      top: 1rem;
      z-index: 100;
      background: rgba(17, 24, 39, 0.95);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 9999px;
      padding: 0.5rem 1rem;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
      margin-bottom: 2rem;
    }}
    .action-bar .brand-badge {{
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      font-size: 0.85rem;
      color: #f8fafc;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      margin-right: 0.4rem;
      padding-right: 0.8rem;
      border-right: 1px solid rgba(255, 255, 255, 0.15);
    }}
    .action-btn {{
      background: rgba(255, 255, 255, 0.08);
      color: #f8fafc;
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 6px;
      padding: 0.4rem 0.8rem;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      text-decoration: none;
      transition: all 0.15s ease;
    }}
    .action-btn:hover {{
      background: rgba(255, 255, 255, 0.16);
      border-color: rgba(255, 255, 255, 0.3);
      transform: translateY(-1px);
    }}
    .action-btn-primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }}
    .action-btn-primary:hover {{
      background: #ff4d56;
      border-color: #ff4d56;
    }}

    /* HOJA DE DOCUMENTO CENTRADA (TIPO LIBRO / PAPER) */
    .document-page {{
      background: var(--paper);
      width: 100%;
      max-width: 820px;
      padding: 3.5rem 3.2rem;
      border-radius: 12px;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.45);
      margin: 0 auto;
    }}
    .doc-header {{
      text-align: center;
      border-bottom: 2px solid var(--border);
      padding-bottom: 1.8rem;
      margin-bottom: 2.2rem;
    }}
    .doc-tag {{
      display: inline-block;
      text-transform: uppercase;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin-bottom: 0.5rem;
    }}
    .doc-title {{
      font-family: 'Outfit', sans-serif;
      font-size: 2rem;
      font-weight: 700;
      color: #0f172a;
      line-height: 1.25;
      margin-bottom: 0.8rem;
    }}
    .doc-meta {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 1.2rem;
      font-size: 0.84rem;
      color: var(--text-muted);
      font-family: 'IBM Plex Mono', monospace;
    }}
    .doc-meta span strong {{
      color: #334155;
    }}
    .doc-content {{
      font-size: 1.12rem;
      line-height: 1.85;
      color: #1e293b;
      text-align: justify;
    }}
    .doc-content p {{
      margin-bottom: 1.4rem;
      text-indent: 1.5rem;
    }}
    .empty-note {{
      text-align: center;
      color: var(--text-muted);
      font-style: italic;
      padding: 3rem 0;
    }}
    .doc-footer {{
      margin-top: 3.5rem;
      padding-top: 1.5rem;
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.75rem;
      color: var(--text-muted);
    }}

    /* RESPONSIVE DESIGN PARA CELULAR / DISPOSITIVOS MÓVILES */
    @media (max-width: 640px) {{
      body {{
        padding: 0.75rem 0.6rem 3rem;
      }}
      .action-bar {{
        position: sticky;
        top: 0.5rem;
        width: 100%;
        max-width: 100%;
        border-radius: 12px;
        flex-wrap: wrap;
        justify-content: center;
        padding: 0.6rem 0.5rem;
        gap: 0.4rem;
        margin-bottom: 1.2rem;
      }}
      .action-bar .brand-badge {{
        width: 100%;
        justify-content: center;
        border-right: none;
        border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        padding-bottom: 0.4rem;
        margin-right: 0;
        padding-right: 0;
        font-size: 0.78rem;
      }}
      .action-btn {{
        flex: 1 1 calc(50% - 0.4rem);
        min-height: 42px;
        justify-content: center;
        font-size: 0.74rem;
        padding: 0.45rem 0.5rem;
      }}
      .document-page {{
        padding: 1.6rem 1.1rem;
        border-radius: 8px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
      }}
      .doc-header {{
        padding-bottom: 1.2rem;
        margin-bottom: 1.5rem;
      }}
      .doc-tag {{
        font-size: 0.68rem;
      }}
      .doc-title {{
        font-size: 1.35rem;
        line-height: 1.3;
      }}
      .doc-meta {{
        gap: 0.4rem 0.8rem;
        font-size: 0.75rem;
      }}
      .doc-content {{
        font-size: 1.0rem;
        line-height: 1.7;
        text-align: left;
      }}
      .doc-content p {{
        margin-bottom: 1.1rem;
        text-indent: 0;
      }}
      .doc-footer {{
        flex-direction: column;
        gap: 0.4rem;
        text-align: center;
        margin-top: 2rem;
        padding-top: 1rem;
      }}
    }}

    /* MEDIA PRINT PARA PDF PERFECTO */
    @media print {{
      body {{
        background: transparent !important;
        padding: 0 !important;
      }}
      .action-bar {{
        display: none !important;
      }}
      .document-page {{
        box-shadow: none !important;
        border-radius: 0 !important;
        padding: 0 !important;
        max-width: 100% !important;
      }}
      @page {{
        margin: 20mm;
        size: A4 portrait;
      }}
    }}
  </style>
</head>
<body>
  <div class="action-bar" role="toolbar" aria-label="Acciones del documento">
    <div class="brand-badge">
      <span>NejoyT</span> · <span>Nerdearla 2026</span>
    </div>
    <button class="action-btn action-btn-primary" onclick="window.print()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
      <span>Imprimir / PDF</span>
    </button>
    <a href="/api/rooms/{room_id}/export?format=pdf&lang={lang}" class="action-btn">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      <span>Descargar PDF</span>
    </a>
    <button class="action-btn" id="btn-copy-ai" onclick="copyForAI()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
      <span id="copy-text">Copiar</span>
    </button>
    <a href="/api/rooms/{room_id}/export?format=txt&lang={lang}" class="action-btn">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
      <span>Descargar .TXT</span>
    </a>
  </div>

  <article class="document-page">
    <header class="doc-header">
      <div class="doc-tag">Transcripción de Conferencia · Nerdearla 2026</div>
      <h1 class="doc-title">{title}</h1>
      <div class="doc-meta">
        <span>Idioma: <strong>{lang_display}</strong></span>
        <span>Audio Origen: <strong>{orig_lang}</strong></span>
        <span>Duración: <strong>{dur_min}m {dur_sec}s</strong></span>
        <span>Palabras: <strong>~{total_words}</strong></span>
      </div>
    </header>

    <main class="doc-content">
      {paragraphs_html}
    </main>

    <footer class="doc-footer">
      <div>NejoyT · Subtitulado Inteligente multi-sala</div>
      <div>Licencia Apache 2.0 · Nerdearla 2026</div>
    </footer>
  </article>

  <script>
    {auto_print_script}

    const cleanSpeechText = {repr(raw_clean_text)};

    async function copyForAI() {{
      const btnText = document.getElementById('copy-text');
      const promptHeader = `Transcripción de ponencia "${title}" (Nerdearla 2026):\\n\\n` + cleanSpeechText;
      try {{
        await navigator.clipboard.writeText(promptHeader);
        btnText.textContent = "¡Copiado!";
        setTimeout(() => {{ btnText.textContent = "Copiar"; }}, 2500);
      }} catch (err) {{
        console.error("No se pudo copiar al portapapeles:", err);
      }}
    }}
  </script>
</body>
</html>
"""


# Singleton del almacén de subtítulos
subtitle_store = SubtitleStore()

