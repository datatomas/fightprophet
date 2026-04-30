#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Streamlit MMA Predictions Dashboard.

Reads pre-materialized Parquet datasets produced by the ETL container
(mma_parquets_dashboard.py).  No DuckDB write access needed — this
container only reads serving Parquets.

Run:
    streamlit run src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py

Env vars:
    PARQUET_BASE_URI  –  base path/URI where the ETL writes parquets
                         (default: ~/db/duck/warehouse/lake)
"""

from __future__ import annotations

import json
import base64
import importlib
import logging
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import quote_plus

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as _st_components

_MARKETING_SITE_ORIGIN = "https://fightprophet.com"
_MARKETING_HOME_URL = f"{_MARKETING_SITE_ORIGIN}/"
_MARKETING_TERMS_URL = f"{_MARKETING_SITE_ORIGIN}/terms/"
_CANONICAL_BY_SLUG = {
    "predictions": f"{_MARKETING_SITE_ORIGIN}/predictions/",
    "upcoming": f"{_MARKETING_SITE_ORIGIN}/predictions/",
    "fight-lab": f"{_MARKETING_SITE_ORIGIN}/fight-lab/",
    "historical-picks": f"{_MARKETING_SITE_ORIGIN}/fight-lab/",
    "model-performance": f"{_MARKETING_SITE_ORIGIN}/fight-lab/",
    "events-history": f"{_MARKETING_SITE_ORIGIN}/events-history/",
    "rankings": f"{_MARKETING_SITE_ORIGIN}/rankings/",
    "belt-holders": f"{_MARKETING_SITE_ORIGIN}/belt-holders/",
    "fighter-profile": f"{_MARKETING_SITE_ORIGIN}/fighter-card/",
    "fighter-card": f"{_MARKETING_SITE_ORIGIN}/fighter-card/",
    "terms": f"{_MARKETING_SITE_ORIGIN}/terms/",
}


def _inject_canonical_link(slug: str) -> None:
    target = _CANONICAL_BY_SLUG.get((slug or "").strip().lower())
    if not target:
        return
    safe_url = target.replace("'", "%27").replace('"', "%22")
    _st_components.html(
        f"""
        <script>
          (function() {{
            try {{
              var doc = window.parent && window.parent.document;
              if (!doc) return;
              var existing = doc.querySelector('link[rel="canonical"]');
              if (existing) existing.parentNode.removeChild(existing);
              var link = doc.createElement('link');
              link.setAttribute('rel', 'canonical');
              link.setAttribute('href', '{safe_url}');
              doc.head.appendChild(link);
            }} catch (e) {{}}
          }})();
        </script>
        """,
        height=0,
    )


def _inject_marketing_handoff_bridge() -> None:
    script = """
        <script>
          (function() {
            try {
              var win = window.parent || window;
              var doc = win.document;
              if (!doc) return;
              var marketingOrigin = __MARKETING_ORIGIN__;

              function ensureHintLink(rel, href, cors) {
                if (!href || !doc.head) return;
                var selector = 'link[rel="' + rel + '"][href="' + href + '"]';
                if (doc.querySelector(selector)) return;
                var link = doc.createElement('link');
                link.rel = rel;
                link.href = href;
                if (cors) link.crossOrigin = 'anonymous';
                doc.head.appendChild(link);
              }

              function primeMarketingOrigin() {
                if (!marketingOrigin) return;
                ensureHintLink('dns-prefetch', marketingOrigin);
                ensureHintLink('preconnect', marketingOrigin, true);
              }

              function primeMarketingHref(href) {
                if (!href || win.__fpSiteWarmHref === href) return;
                win.__fpSiteWarmHref = href;
                try {
                  win.fetch(href, {
                    mode: 'no-cors',
                    credentials: 'omit',
                    cache: 'force-cache',
                    keepalive: true,
                  }).catch(function() {});
                } catch (e) {}
              }

              primeMarketingOrigin();

              if (win.__fpSiteHandoffBound) return;
              win.__fpSiteHandoffBound = true;

              doc.addEventListener('pointerdown', function(event) {
                var target = event.target;
                if (!target || !target.closest) return;
                var anchor = target.closest('a.fp-site-shell-link[href]');
                if (!anchor) return;
                primeMarketingOrigin();
                primeMarketingHref(anchor.href || anchor.getAttribute('href'));
              }, true);

              doc.addEventListener('pointerover', function(event) {
                var target = event.target;
                if (!target || !target.closest) return;
                var anchor = target.closest('a.fp-site-shell-link[href]');
                if (!anchor) return;
                primeMarketingOrigin();
                primeMarketingHref(anchor.href || anchor.getAttribute('href'));
              }, true);
            } catch (e) {}
          })();
        </script>
        """
    _st_components.html(
        script.replace("__MARKETING_ORIGIN__", json.dumps(_MARKETING_SITE_ORIGIN)),
        height=0,
    )

try:
    stx = importlib.import_module("extra_streamlit_components")
except Exception:
    stx = None

try:
    _st_aggrid = importlib.import_module("st_aggrid")
    AgGrid = getattr(_st_aggrid, "AgGrid", None)
    GridOptionsBuilder = getattr(_st_aggrid, "GridOptionsBuilder", None)
except Exception:
    AgGrid = None
    GridOptionsBuilder = None

logger = logging.getLogger(__name__)

_PAGE_SLUG_ALIASES = {
    "fighter-profile": "fighter-card",
}

# ---------------------------------------------------------------------------
# News widget (RSS, sidebar + upcoming page)
# ---------------------------------------------------------------------------

_FEEDS_PATH = Path(__file__).parent / "news_feeds.json"
_COOKIE_PAGE_SLUG = "fp_page_slug"
_COOKIE_SOURCE_MODE = "fp_data_source_mode"
_COOKIE_PARQUET_PREFIX = "fp_parquet_prefix"
_COOKIE_SELECTED_FIGHTER = "fp_selected_fighter"
_COOKIE_IMAGE_MODE = "fp_image_mode"
_COOKIE_LANG = "fp_lang"


def _cookie_manager():
    if stx is None:
        return None
    key = "_fp_cookie_manager"
    existing = st.session_state.get(key)
    if existing is not None:
        return existing
    try:
        manager = stx.CookieManager()
        st.session_state[key] = manager
        return manager
    except Exception:
        return None


def _cookie_get(name: str, default: str = "") -> str:
    manager = _cookie_manager()
    if manager is None:
        return default
    try:
        val = manager.get(cookie=name)
        if val is None:
            return default
        return str(val)
    except Exception:
        return default


def _cookie_set(name: str, value: str) -> None:
    manager = _cookie_manager()
    if manager is None:
        return
    try:
        manager.set(name, value)
    except Exception:
        pass


_LANG_CODE_TO_LABEL = {
    "en": "English",
    "es": "Español",
    "pt": "Português",
}
_LANG_LABEL_TO_CODE = {v: k for k, v in _LANG_CODE_TO_LABEL.items()}
_LANG_BUTTONS = {
    "es": "🇨🇴",
    "pt": "🇧🇷",
    "en": "🇺🇸",
}

STRINGS = {
    "news.latest": "Latest MMA News",
    "sidebar.language": "Language",
    "sidebar.title": "Bet wisely with Fight Prophet",
    "sidebar.caption": "AI-powered MMA fight predictions",
    "sidebar.navigate": "Navigate",
    "nav.home": "Home & Rankings",
    "nav.terms": "Terms & Conditions",
    "nav.predictions": "Predictions",
    "nav.model_performance": "Fight Lab",
    "nav.historical": "Fight Lab",
    "nav.rankings": "Fighter Rankings",
    "nav.events_history": "Events History",
    "nav.fighter_profile": "Fighter Cards",
    "nav.belt_holders": "Belt Holders",
    "sidebar.data_source_mode": "Data source mode",
    "sidebar.data_source_help": "auto = detect from env, azure = force blob, local = force disk",
    "sidebar.mode.auto": "auto",
    "sidebar.mode.azure": "azure",
    "sidebar.mode.local": "local",
    "sidebar.parquet_prefix": "Parquet prefix",
    "sidebar.parquet_prefix_help": "Folder prefix inside the base (e.g. mma/diamond). Must match where the ETL wrote files.",
    "sidebar.image_loading": "Fighter image loading",
    "sidebar.image_loading_help": "off = no fighter photos, smart = photos only where most useful, all = photos everywhere",
    "sidebar.image_mode.off": "off",
    "sidebar.image_mode.smart": "smart",
    "sidebar.image_mode.all": "all",
    "sidebar.mode_summary": "**Mode:** `{mode}`  \n**Source:** {source}  \n**Base:** `{base}/`  \n**Prefix:** `{prefix}`",
    "source.azure_blob": "Azure Blob",
    "source.parquet_lake": "Parquet lake",
    "sidebar.test_azure": "Test Azure connection",
    "sidebar.test_azure_help": "Verify credentials and list available folders",
    "sidebar.connecting": "Connecting…",
    "sidebar.azure_missing_creds": "AZURE_STORAGE_ACCOUNT or AZURE_STORAGE_KEY not set.",
    "sidebar.azure_connected": "Connected to `{container}`. Folders under `/{prefix}`:",
    "sidebar.no_folders": "No folders found under `{prefix}`. Is the prefix correct?",
    "sidebar.connection_failed": "Connection failed: {error}",
    "page.home.title": "Welcome to Fight Prophet",
    "page.home.body": """
Fight Prophet provides AI-assisted MMA prediction insights, fighter analytics, and model performance views.

Use the sidebar to navigate:
- Upcoming Predictions
- Fight Lab (historical picks + model diagnostics)
- Fighter Rankings
- Fighter Cards
""",
    "common.contact": "Contact",
    "page.terms.title": "Terms & Conditions",
    "page.terms.body": """
By using this dashboard, you acknowledge and agree to the following:

1. **Informational use only**  
    All model outputs, rankings, and suggestions are provided for informational and educational purposes only.

2. **No financial advice**  
    Nothing in this product constitutes financial, investment, betting, legal, or professional advice.

3. **No guarantees**  
    Predictions are probabilistic and may be incorrect. Past performance does not guarantee future results.

4. **User responsibility**  
    You are solely responsible for any decisions, actions, or wagers you make based on this dashboard.

5. **Data/model limitations**  
    Data quality, availability, and model assumptions may affect outputs. Errors, delays, or omissions may occur.

6. **Liability limitation**  
    UpperCut Analytics and contributors are not liable for losses or damages arising from use of this dashboard.

7. **Changes**  
    Terms may be updated over time. Continued use implies acceptance of the latest version.
""",
    "page.upcoming.title": "Upcoming Fight Predictions",
    "page.upcoming.prediction_model": "Prediction model",
    "page.upcoming.no_data": "No upcoming fights available. The ETL may not have run yet.",
    "page.upcoming.load_all": "Load all upcoming predictions",
    "page.upcoming.load_all_help": "Default stays on the next scheduled event for speed. Turn this on only when you want the full upcoming slate.",
    "page.upcoming.showing_next_event": "Showing the next scheduled event by default for faster load.",
    "page.upcoming.closest_event": "Closest Event",
    "page.upcoming.all_events": "All Events",
    "page.upcoming.filter_event": "Filter by event",
    "page.upcoming.total_fights": "Total Fights",
    "page.upcoming.upcoming_fights": "Upcoming",
    "page.upcoming.analyzed_fights": "Analyzed",
    "page.upcoming.events": "Events",
    "page.upcoming.strong_signals": "Strong Signals",
    "page.upcoming.recommended_bets": "Value Flags",
    "page.upcoming.model_prediction_win": "Model prediction to win",
    "page.upcoming.best_value_bet": "Top value angle",
    "page.upcoming.underdog_value_angle": "Underdog value angle",
    "page.upcoming.edge": "Edge",
    "page.upcoming.market": "Market",
    "page.upcoming.signal": "Signal",
    "page.upcoming.threshold_passed": "Value flag",
    "page.upcoming.model_probabilities": "Model probabilities",
    "page.upcoming.value_signal_caption": "Value flag is a market-pricing signal, not advice or a safe outcome prediction.",
    "page.events_history.title": "Events History",
    "page.events_history.no_data": "No events history available from exported datasets yet.",
    "page.belt_holders.title": "Belt Holders",
    "page.belt_holders.no_data": "No belt holders data available. Run the belt holders ETL first.",
    "page.belt_holders.current_champions": "Current Champions",
    "page.belt_holders.title_fight_history": "Title Fight History",
    "page.belt_holders.weight_class": "Weight Class",
    "page.belt_holders.champion": "Champion",
    "page.belt_holders.title_won": "Title Won",
    "page.belt_holders.defenses": "Defenses",
    "page.belt_holders.last_title_fight": "Last Title Fight",
    "page.belt_holders.vacant": "VACANT",
    "page.belt_holders.all_divisions": "All Divisions",
    "page.belt_holders.filter_division": "Filter by division",
    "page.belt_holders.total_divisions": "Divisions",
    "page.belt_holders.active_champions": "Active Champions",
    "page.belt_holders.total_title_fights": "Title Fights",
    "page.belt_holders.title_changes": "Title Changes",
    "page.belt_holders.manual_overrides": "Manual Belt Overrides",
    "page.belt_holders.manual_overrides_empty": "No manual override dataset found.",
}

STATIC_TRANSLATIONS = {
    "es": {
        "news.latest": "Últimas noticias de MMA",
        "sidebar.language": "Idioma",
        "sidebar.title": "Apuesta con inteligencia con Fight Prophet",
        "sidebar.caption": "Predicciones de MMA impulsadas por IA",
        "sidebar.navigate": "Navegar",
        "nav.home": "Inicio y Rankings",
        "nav.terms": "Términos y Condiciones",
        "nav.predictions": "Predicciones",
        "nav.model_performance": "Fight Lab",
        "nav.historical": "Fight Lab",
        "nav.rankings": "Rankings de Peleadores",
        "nav.events_history": "Historial de Eventos",
        "nav.fighter_profile": "Tarjetas de Peleadores",
        "nav.belt_holders": "Poseedores del Cinturón",
        "sidebar.data_source_mode": "Modo de fuente de datos",
        "sidebar.data_source_help": "auto = detectar desde entorno, azure = forzar blob, local = forzar disco",
        "sidebar.mode.auto": "auto",
        "sidebar.mode.azure": "azure",
        "sidebar.mode.local": "local",
        "sidebar.parquet_prefix": "Prefijo Parquet",
        "sidebar.parquet_prefix_help": "Prefijo de carpeta dentro de la base (ej. mma/diamond). Debe coincidir con donde ETL escribió archivos.",
        "sidebar.image_loading": "Carga de imágenes de peleadores",
        "sidebar.image_loading_help": "off = sin fotos, smart = fotos solo donde más útil, all = fotos en todas partes",
        "sidebar.image_mode.off": "off",
        "sidebar.image_mode.smart": "smart",
        "sidebar.image_mode.all": "all",
        "source.azure_blob": "Azure Blob",
        "source.parquet_lake": "Lago Parquet",
        "sidebar.test_azure": "Probar conexión Azure",
        "sidebar.test_azure_help": "Verificar credenciales y listar carpetas disponibles",
        "sidebar.connecting": "Conectando…",
        "sidebar.azure_missing_creds": "AZURE_STORAGE_ACCOUNT o AZURE_STORAGE_KEY no están configurados.",
        "sidebar.no_folders": "No se encontraron carpetas en `{prefix}`. ¿El prefijo es correcto?",
        "sidebar.connection_failed": "Conexión fallida: {error}",
        "page.home.title": "Bienvenido a Fight Prophet",
        "page.home.body": """
Fight Prophet ofrece insights de predicción MMA asistidos por IA, analítica de peleadores y vistas de rendimiento del modelo.

Usa la barra lateral para navegar:
- Próximas Predicciones
- Rendimiento del Modelo
- Picks Históricos
- Rankings de Peleadores
- Tarjetas de Peleadores
""",
        "common.contact": "Contacto",
        "page.terms.title": "Términos y Condiciones",
        "page.upcoming.title": "Próximas Predicciones de Peleas",
        "page.upcoming.prediction_model": "Modelo de predicción",
        "page.upcoming.no_data": "No hay peleas próximas disponibles. Es posible que el ETL no se haya ejecutado aún.",
        "page.upcoming.load_all": "Cargar todas las predicciones próximas",
        "page.upcoming.load_all_help": "Por velocidad, la vista inicial muestra solo el próximo evento programado. Actívalo solo si quieres ver toda la cartelera futura.",
        "page.upcoming.showing_next_event": "Mostrando el próximo evento programado por defecto para una carga más rápida.",
        "page.upcoming.closest_event": "Evento más cercano",
        "page.upcoming.all_events": "Todos los eventos",
        "page.upcoming.filter_event": "Filtrar por evento",
        "page.upcoming.total_fights": "Total de peleas",
        "page.upcoming.upcoming_fights": "Próximas",
        "page.upcoming.analyzed_fights": "Analizadas",
        "page.upcoming.events": "Eventos",
        "page.upcoming.strong_signals": "Señales fuertes",
        "page.upcoming.recommended_bets": "Señales de valor",
        "page.upcoming.model_prediction_win": "Predicción del modelo para ganar",
        "page.upcoming.best_value_bet": "Mejor ángulo de valor",
        "page.upcoming.underdog_value_angle": "Ángulo de valor del no favorito",
        "page.upcoming.edge": "Ventaja",
        "page.upcoming.market": "Mercado",
        "page.upcoming.signal": "Señal",
        "page.upcoming.threshold_passed": "Señal de valor",
        "page.upcoming.model_probabilities": "Probabilidades del modelo",
        "page.upcoming.value_signal_caption": "La señal de valor es una lectura de precio de mercado, no consejo ni una predicción segura del resultado.",
        "page.events_history.title": "Historial de Eventos",
        "page.events_history.no_data": "Aún no hay historial de eventos disponible en los datasets exportados.",
        "page.belt_holders.title": "Poseedores del Cinturón",
        "page.belt_holders.no_data": "No hay datos de poseedores del cinturón. Ejecute primero el ETL de cinturones.",
        "page.belt_holders.current_champions": "Campeones Actuales",
        "page.belt_holders.title_fight_history": "Historial de Peleas por el Título",
        "page.belt_holders.weight_class": "Categoría de Peso",
        "page.belt_holders.champion": "Campeón",
        "page.belt_holders.title_won": "Título Ganado",
        "page.belt_holders.defenses": "Defensas",
        "page.belt_holders.last_title_fight": "Última Pelea por el Título",
        "page.belt_holders.vacant": "VACANTE",
        "page.belt_holders.all_divisions": "Todas las Divisiones",
        "page.belt_holders.filter_division": "Filtrar por división",
        "page.belt_holders.total_divisions": "Divisiones",
        "page.belt_holders.active_champions": "Campeones Activos",
        "page.belt_holders.total_title_fights": "Peleas por el Título",
        "page.belt_holders.title_changes": "Cambios de Título",
    },
    "pt": {
        "news.latest": "Últimas notícias de MMA",
        "sidebar.language": "Idioma",
        "sidebar.title": "Aposte com inteligência com Fight Prophet",
        "sidebar.caption": "Previsões de lutas de MMA com IA",
        "sidebar.navigate": "Navegar",
        "nav.home": "Início e Rankings",
        "nav.terms": "Termos e Condições",
        "nav.predictions": "Previsões",
        "nav.model_performance": "Fight Lab",
        "nav.historical": "Fight Lab",
        "nav.rankings": "Rankings de Lutadores",
        "nav.events_history": "Histórico de Eventos",
        "nav.fighter_profile": "Cards de Lutadores",
        "nav.belt_holders": "Detentores do Cinturão",
        "sidebar.data_source_mode": "Modo da fonte de dados",
        "sidebar.data_source_help": "auto = detectar pelo ambiente, azure = forçar blob, local = forçar disco",
        "sidebar.mode.auto": "auto",
        "sidebar.mode.azure": "azure",
        "sidebar.mode.local": "local",
        "sidebar.parquet_prefix": "Prefixo Parquet",
        "sidebar.parquet_prefix_help": "Prefixo da pasta dentro da base (ex. mma/diamond). Deve corresponder ao local onde o ETL gravou os arquivos.",
        "sidebar.image_loading": "Carregamento de imagens de lutadores",
        "sidebar.image_loading_help": "off = sem fotos, smart = fotos só onde é mais útil, all = fotos em todo lugar",
        "sidebar.image_mode.off": "off",
        "sidebar.image_mode.smart": "smart",
        "sidebar.image_mode.all": "all",
        "source.azure_blob": "Azure Blob",
        "source.parquet_lake": "Lago Parquet",
        "sidebar.test_azure": "Testar conexão Azure",
        "sidebar.test_azure_help": "Verificar credenciais e listar pastas disponíveis",
        "sidebar.connecting": "Conectando…",
        "sidebar.azure_missing_creds": "AZURE_STORAGE_ACCOUNT ou AZURE_STORAGE_KEY não estão definidos.",
        "sidebar.no_folders": "Nenhuma pasta encontrada em `{prefix}`. O prefixo está correto?",
        "sidebar.connection_failed": "Falha na conexão: {error}",
        "page.home.title": "Bem-vindo ao Fight Prophet",
        "page.home.body": """
Fight Prophet fornece insights de previsão de MMA com IA, análises de lutadores e visões de desempenho do modelo.

Use a barra lateral para navegar:
- Próximas Previsões
- Desempenho do Modelo
- Picks Históricos
- Rankings de Lutadores
- Cards de Lutadores
""",
        "common.contact": "Contato",
        "page.terms.title": "Termos e Condições",
        "page.upcoming.title": "Próximas Previsões de Lutas",
        "page.upcoming.prediction_model": "Modelo de previsão",
        "page.upcoming.no_data": "Não há lutas futuras disponíveis. O ETL pode não ter sido executado ainda.",
        "page.upcoming.load_all": "Carregar todas as previsões futuras",
        "page.upcoming.load_all_help": "Por velocidade, a visualização padrão mostra apenas o próximo evento agendado. Ative isto somente se quiser ver toda a lista futura.",
        "page.upcoming.showing_next_event": "Mostrando por padrão o próximo evento agendado para carregar mais rápido.",
        "page.upcoming.closest_event": "Evento mais próximo",
        "page.upcoming.all_events": "Todos os eventos",
        "page.upcoming.filter_event": "Filtrar por evento",
        "page.upcoming.total_fights": "Total de lutas",
        "page.upcoming.upcoming_fights": "Próximas",
        "page.upcoming.analyzed_fights": "Analisadas",
        "page.upcoming.events": "Eventos",
        "page.upcoming.strong_signals": "Sinais fortes",
        "page.upcoming.recommended_bets": "Sinais de valor",
        "page.upcoming.model_prediction_win": "Previsão do modelo para vencer",
        "page.upcoming.best_value_bet": "Melhor ângulo de valor",
        "page.upcoming.underdog_value_angle": "Ângulo de valor do azarão",
        "page.upcoming.edge": "Vantagem",
        "page.upcoming.market": "Mercado",
        "page.upcoming.signal": "Sinal",
        "page.upcoming.threshold_passed": "Sinal de valor",
        "page.upcoming.model_probabilities": "Probabilidades do modelo",
        "page.upcoming.value_signal_caption": "O sinal de valor é uma leitura de preço de mercado, não conselho nem previsão segura do resultado.",
        "page.events_history.title": "Histórico de Eventos",
        "page.events_history.no_data": "Ainda não há histórico de eventos disponível nos datasets exportados.",
        "page.belt_holders.title": "Detentores do Cinturão",
        "page.belt_holders.no_data": "Não há dados de detentores do cinturão. Execute o ETL de cinturões primeiro.",
        "page.belt_holders.current_champions": "Campeões Atuais",
        "page.belt_holders.title_fight_history": "Histórico de Lutas pelo Título",
        "page.belt_holders.weight_class": "Categoria de Peso",
        "page.belt_holders.champion": "Campeão",
        "page.belt_holders.title_won": "Título Conquistado",
        "page.belt_holders.defenses": "Defesas",
        "page.belt_holders.last_title_fight": "Última Luta pelo Título",
        "page.belt_holders.vacant": "VAGO",
        "page.belt_holders.all_divisions": "Todas as Divisões",
        "page.belt_holders.filter_division": "Filtrar por divisão",
        "page.belt_holders.total_divisions": "Divisões",
        "page.belt_holders.active_champions": "Campeões Ativos",
        "page.belt_holders.total_title_fights": "Lutas pelo Título",
        "page.belt_holders.title_changes": "Mudanças de Título",
    },
}


def _normalize_lang(value: str) -> str:
    val = (value or "").strip().lower()
    aliases = {
        "en": "en",
        "english": "en",
        "es": "es",
        "espanol": "es",
        "español": "es",
        "pt": "pt",
        "portuguese": "pt",
        "português": "pt",
    }
    return aliases.get(val, "")


def _resolve_ui_lang() -> str:
    requested = _normalize_lang(str(st.query_params.get("lang", "")))
    if requested:
        return requested
    cookie_lang = _normalize_lang(_cookie_get(_COOKIE_LANG, ""))
    if cookie_lang:
        return cookie_lang
    env_lang = _normalize_lang(os.environ.get("DEFAULT_UI_LANG", ""))
    if env_lang:
        return env_lang
    return "en"


@st.cache_resource
def _get_translator(lang: str):
    if lang not in {"es", "pt"}:
        return None
    try:
        deep_translator = importlib.import_module("deep_translator")
        GoogleTranslator = getattr(deep_translator, "GoogleTranslator")
        return GoogleTranslator(source="en", target=lang)
    except Exception:
        return None


@st.cache_data(ttl=86400, show_spinner=False, max_entries=4096)
def _translate_text_cached(lang: str, text: str) -> str:
    if lang == "en" or not text:
        return text
    translator = _get_translator(lang)
    if translator is None:
        return text
    try:
        return translator.translate(text)
    except Exception:
        return text


def t(key: str, **kwargs) -> str:
    lang = _normalize_lang(st.session_state.get("ui_lang", "en")) or "en"
    english = STRINGS.get(key, key)
    if lang == "en":
        template = english
    else:
        template = (
            STATIC_TRANSLATIONS.get(lang, {}).get(key)
            or _translate_text_cached(lang, english)
            or english
        )
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return english
    return template


def _render_lang_flag_selector(current_lang: str) -> str:
    cols = st.columns(3)
    selected = current_lang if current_lang in _LANG_BUTTONS else "en"
    for idx, lang_code in enumerate(("es", "pt", "en")):
        label = _LANG_BUTTONS[lang_code]
        if lang_code == selected:
            label = f"{label} ✓"
        if cols[idx].button(label, key=f"lang_flag_{lang_code}", use_container_width=True):
            selected = lang_code
    return selected


def _render_sidebar_lang_switch(current_lang: str, page_slug: str) -> str:
    selected = current_lang if current_lang in _LANG_BUTTONS else "en"
    safe_page = (page_slug or "predictions").strip().lower() or "predictions"
    links: list[str] = []
    for lang_code in ("es", "pt", "en"):
        classes = "fp-sidebar-lang-btn is-active" if lang_code == selected else "fp-sidebar-lang-btn"
        href = f"?page={quote_plus(safe_page)}&lang={quote_plus(lang_code)}"
        label = _LANG_BUTTONS[lang_code]
        title = escape(_LANG_CODE_TO_LABEL.get(lang_code, lang_code))
        links.append(
            f'<a class="{classes}" href="{escape(href, quote=True)}" title="{title}" aria-label="{title}">{escape(label)}</a>'
        )
    st.markdown(
        '<div class="fp-sidebar-lang-switch" aria-label="Language selector">'
        + "".join(links)
        + "</div>",
        unsafe_allow_html=True,
    )
    return selected


def _render_mma_news(*, location: str = "sidebar", limit: int = 6) -> None:
    """Lightweight MMA RSS headlines for SEO/context. Silent on failures."""
    try:
        feed_list = json.loads(_FEEDS_PATH.read_text())["feeds"]
        feeds = {f["name"]: f["url"] for f in feed_list}
    except Exception:
        feeds = {}
    if not feeds:
        return
    try:
        import feedparser  # already in requirements.front.txt
    except ImportError:
        return

    @st.cache_data(ttl=3600, show_spinner=False)
    def _fetch() -> list[dict]:
        items: list[dict] = []
        per = max(1, limit // max(1, len(feeds)))
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        for source, url in feeds.items():
            try:
                feed = feedparser.parse(url, request_headers={"User-Agent": ua})
                for entry in feed.entries[:per]:
                    title = (entry.get("title") or "").strip()
                    link = (entry.get("link") or "").strip()
                    if not title or not link:
                        continue
                    pub = (entry.get("published") or entry.get("updated") or "").strip()
                    pub = pub[:16] if len(pub) > 16 else pub
                    items.append({"title": title, "link": link, "source": source, "date": pub})
            except Exception:
                continue
        return items

    articles = _fetch()
    if not articles:
        return
    ctx = st.sidebar if location == "sidebar" else st
    expander_label = t("news.latest")
    with ctx.expander(expander_label, expanded=False):
        for a in articles:
            st.markdown(f"[{a['title']}]({a['link']}) — {a['source']} · {a['date']}")

# ---------------------------------------------------------------------------
# Env / path helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
_STATIC_DIR = _PROJECT_ROOT / "static"
_GOAT_EMOJI_PNG_PATH = _STATIC_DIR / "b91c1c-goat-emoji.png"
_NAV_ICON_FILES = {
    "predictions": "b91c1c-predictions-emoji-rail.png",
    "fighter-card": "b91c1c-fighterscard-emoji-rail.png",
    "belt-holders": "b91c1c-belt-emoji-rail.png",
    "events-history": "b91c1c-events-emoji-rail.png",
    "rankings": "b91c1c-goat-emoji-rail.png",
    "fight-lab": "b91c1c-lab-emoji-rail.png",
    "terms": "b91c1c-terms-emoji-rail.png",
}

from ml_kuda_sports_lab.front_end.country_master import (
    canonical_country_name as _canonical_country_name,
    country_flag as _shared_country_flag,
    country_iso2 as _shared_country_iso2,
    country_short_label as _shared_country_short_label,
)

try:
    from ml_kuda_sports_lab.envloader import load_env
    load_env()
except Exception:
    pass


def _get_parquet_base() -> str:
    """Resolve the base path where dashboard Parquets live."""
    env = os.environ.get("PARQUET_BASE_URI")
    if env:
        return env.rstrip("/")
    # Fallback: next to the DuckDB warehouse dir (matches ETL default)
    for var in ("DUCK_DEV_DB", "DUCK_WH_DB", "DUCKDB_PATH"):
        p = os.environ.get(var)
        if p and Path(p).exists():
            return str(Path(p).parent / "lake")
    return str(Path.home() / "db" / "duck" / "warehouse" / "lake")


def _get_local_fallback_base() -> str:
    """Local parquet fallback path used when Azure read fails."""
    local_env = os.environ.get("LOCAL_PARQUET_BASE_URI", "").strip()
    if local_env:
        return local_env.rstrip("/")
    for var in ("DUCK_DEV_DB", "DUCK_WH_DB", "DUCKDB_PATH"):
        p = os.environ.get(var)
        if p and Path(p).exists():
            return str(Path(p).parent / "lake")
    return str(Path.home() / "db" / "duck" / "warehouse" / "lake")


def _get_source_mode_default() -> str:
    """Resolve default frontend source mode from env."""
    mode = os.environ.get("PARQUET_SOURCE", "auto").strip().lower()
    if mode not in {"auto", "azure", "local"}:
        return "auto"
    return mode


def _resolve_source(mode: str) -> tuple[str, str]:
    """Resolve (base_uri, source_label) from selected mode.

    Modes:
      - auto:   PARQUET_BASE_URI > Azure (if creds present) > local fallback
      - azure:  AZURE_PARQUET_BASE_URI or az://<AZURE_STORAGE_CONTAINER>
      - local:  LOCAL_PARQUET_BASE_URI or default local fallback
    """
    mode = (mode or "auto").strip().lower()

    if mode == "azure":
        azure_base = os.environ.get("AZURE_PARQUET_BASE_URI", "").strip()
        if azure_base:
            return azure_base.rstrip("/"), "Azure Blob"
        container = os.environ.get("AZURE_STORAGE_CONTAINER", "fightprophet-dashboard").strip()
        return f"az://{container}", "Azure Blob"

    if mode == "local":
        local_base = os.environ.get("LOCAL_PARQUET_BASE_URI", "").strip()
        if local_base:
            return local_base.rstrip("/"), "Parquet lake"
        return _get_parquet_base(), "Parquet lake"

    # auto mode
    explicit = os.environ.get("PARQUET_BASE_URI", "").strip()
    if explicit:
        base = explicit.rstrip("/")
        return base, ("Azure Blob" if _is_azure(base) else "Parquet lake")

    if os.environ.get("AZURE_STORAGE_ACCOUNT") and os.environ.get("AZURE_STORAGE_KEY"):
        container = os.environ.get("AZURE_STORAGE_CONTAINER", "fightprophet-dashboard").strip()
        return f"az://{container}", "Azure Blob"

    return _get_parquet_base(), "Parquet lake"


def _get_parquet_prefix() -> str:
    """Optional serving prefix under PARQUET_BASE_URI (e.g. mma/diamond)."""
    # Default to mma/diamond — the standard layout for this project.
    return os.environ.get("PARQUET_PREFIX", "mma/diamond").strip().strip("/")


def _get_front_cache_ttl_seconds() -> int:
    raw = os.environ.get("FRONT_CACHE_TTL_SECONDS", "300").strip()
    try:
        return max(60, int(raw))
    except Exception:
        return 300


def _get_image_public_mode() -> str:
    """Resolve image URL access mode: auto | on | off."""
    mode = os.environ.get("FIGHTER_IMAGES_PUBLIC_MODE", "auto").strip().lower()
    return mode if mode in {"auto", "on", "off"} else "auto"


def _apply_prefix(folder: str, prefix: str | None = None) -> str:
    if prefix is None:
        prefix = _get_parquet_prefix()
    prefix = prefix.strip("/")
    if not prefix:
        return folder
    return f"{prefix}/{folder}"


def _is_azure(uri: str) -> bool:
    return uri.startswith("az://") or uri.startswith("azure://")


def _get_fighter_images_base_url() -> str:
    """Resolve base URL for fighter headshots hosted in Azure Blob Storage."""
    env = os.environ.get("FIGHTER_IMAGES_BASE_URL", "").strip()
    if env:
        return env.rstrip("/")
    return "https://stfightprophetetldev01.blob.core.windows.net/fightprophet-statics/fighters"


def _sanitize_name_for_image(name: object) -> str:
    """Convert fighter display name into blob-safe token used by image folders/files."""
    raw = "" if name is None else str(name).strip()
    if not raw:
        return ""
    token = raw.replace(" ", "_")
    token = re.sub(r"[^A-Za-z0-9_\-]", "", token)
    token = re.sub(r"_+", "_", token)
    return token.strip("_")


def _candidate_name_tokens(name: object) -> list[str]:
    """Generate candidate tokens for fighter image naming variations."""
    raw = "" if name is None else str(name).strip()
    if not raw:
        return []

    candidates: list[str] = []
    direct = _sanitize_name_for_image(raw)
    if direct:
        candidates.append(direct)

    without_parens = re.sub(r"\s*\([^)]*\)", "", raw).strip()
    token_no_parens = _sanitize_name_for_image(without_parens)
    if token_no_parens and token_no_parens not in candidates:
        candidates.append(token_no_parens)

    raw_no_hyphen = raw.replace("-", "_")
    token_no_hyphen = _sanitize_name_for_image(raw_no_hyphen)
    if token_no_hyphen and token_no_hyphen not in candidates:
        candidates.append(token_no_hyphen)

    return candidates


def _candidate_folder_tokens(name: object) -> list[str]:
    """Generate candidate folder tokens (can include parentheses)."""
    raw = "" if name is None else str(name).strip()
    if not raw:
        return []

    folder_candidates: list[str] = []

    # Keep parentheses for folder variant: Alex_Caceres_(Bruce_Leeroy)
    with_parens = raw.replace(" ", "_")
    with_parens = re.sub(r"[^A-Za-z0-9_\-()]", "", with_parens)
    with_parens = re.sub(r"_+", "_", with_parens).strip("_")
    if with_parens:
        folder_candidates.append(with_parens)

    # Also try stripped/normalized variants
    for token in _candidate_name_tokens(raw):
        if token not in folder_candidates:
            folder_candidates.append(token)

    return folder_candidates


def _normalize_fighter_id(fid: object | None) -> str | None:
    """Normalize fighter id; prefer substring starting at 'ufc_' when present."""
    if fid is None:
        return None
    txt = str(fid).strip()
    if not txt:
        return None
    low = txt.lower()
    idx = low.find("ufc_")
    if idx >= 0:
        return txt[idx:]
    return txt


@st.cache_data(ttl=3600, show_spinner=False)
def _url_exists(url: str) -> bool:
    """Check whether a public URL exists (HEAD first, GET fallback)."""
    try:
        req = request.Request(url, method="HEAD")
        with request.urlopen(req, timeout=2.5) as resp:
            return int(getattr(resp, "status", 200)) < 400
    except HTTPError as exc:
        if exc.code in (403, 405):
            # Some blobs / proxies block HEAD; try lightweight GET.
            try:
                req_get = request.Request(url, headers={"Range": "bytes=0-0"})
                with request.urlopen(req_get, timeout=3.0) as resp:
                    return int(getattr(resp, "status", 200)) < 400
            except Exception:
                return False
        return False
    except URLError:
        return False
    except Exception:
        return False


def _parse_assets_base_url(base_url: str) -> tuple[str, str, str] | None:
    """Parse assets URL into (account, container, prefix_inside_container)."""
    try:
        parsed = urlparse(base_url)
        host = (parsed.netloc or "").strip().lower()
        path = (parsed.path or "").strip("/")
        if not host.endswith(".blob.core.windows.net"):
            return None
        account = host.split(".blob.core.windows.net", 1)[0]
        if not account or not path:
            return None
        parts = path.split("/", 1)
        container = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        return account, container, prefix
    except Exception:
        return None


@st.cache_resource
def _get_assets_container_client(base_url: str):
    """Get Azure ContainerClient for fighter assets when account/key are available."""
    parsed = _parse_assets_base_url(base_url)
    if not parsed:
        return None
    account_from_url, container, _ = parsed

    account = (
        os.environ.get("FIGHTER_IMAGES_STORAGE_ACCOUNT", "").strip()
        or account_from_url
    )
    key = (
        os.environ.get("FIGHTER_IMAGES_STORAGE_KEY", "").strip()
        or os.environ.get("AZURE_STORAGE_KEY", "").strip()
    )
    if not account or not key:
        return None

    try:
        from azure.storage.blob import BlobServiceClient

        client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
        return client.get_container_client(container)
    except Exception:
        return None


@st.cache_resource
def _prefetch_image_index(base_url: str) -> set[str]:
    """Build a set of all known blob paths once at startup (survives reruns)."""
    container_client = _get_assets_container_client(base_url)
    if container_client is None:
        return set()
    known: set[str] = set()
    try:
        for blob in container_client.list_blob_names():
            known.add(str(blob))
    except Exception:
        pass
    return known


def _url_exists_fast(base_url: str, rel_path: str) -> bool:
    """Check blob existence via in-memory index; falls back to live HEAD only when index is empty."""
    index = _prefetch_image_index(base_url)
    if index:
        parsed = _parse_assets_base_url(base_url)
        if not parsed:
            return False
        _, _, prefix = parsed
        full_path = f"{prefix.rstrip('/')}/{rel_path.lstrip('/')}" if prefix else rel_path.lstrip("/")
        return full_path in index
    # Index unavailable (no SDK creds) — fall back to HTTP HEAD
    return _url_exists(rel_path)


@st.cache_data(ttl=1800, show_spinner=False)
def _build_blob_read_url(base_url: str, blob_rel_path: str) -> str | None:
    """Return public URL if accessible, else a SAS URL for private blobs when possible."""
    direct_url = f"{base_url.rstrip('/')}/{blob_rel_path.lstrip('/')}"
    public_mode = _get_image_public_mode()
    # Prefer image-specific credentials when configured.
    key = (
        os.environ.get("FIGHTER_IMAGES_STORAGE_KEY", "").strip()
        or os.environ.get("AZURE_STORAGE_KEY", "").strip()
    )
    should_try_public_first = (
        public_mode == "on"
        or (public_mode == "auto" and not key)
    )
    if should_try_public_first and _url_exists_fast(base_url, blob_rel_path):
        return direct_url

    parsed = _parse_assets_base_url(base_url)
    if not parsed:
        return None
    account_from_url, container, prefix = parsed
    account = (
        os.environ.get("FIGHTER_IMAGES_STORAGE_ACCOUNT", "").strip()
        or account_from_url
    )
    if not account or not key:
        if public_mode in {"auto", "off"} and _url_exists_fast(base_url, blob_rel_path):
            return direct_url
        return None

    try:
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions

        blob_path = f"{prefix.rstrip('/')}/{blob_rel_path.lstrip('/')}" if prefix else blob_rel_path.lstrip("/")
        index = _prefetch_image_index(base_url)
        if index and blob_path not in index:
            return None
        if not index:
            # No index available — fall back to SDK existence check
            container_client = _get_assets_container_client(base_url)
            if container_client is None:
                return None
            blob_client = container_client.get_blob_client(blob_path)
            if not blob_client.exists():
                return None

        expiry_hours = int(os.environ.get("FIGHTER_IMAGE_SAS_HOURS", "24") or "24")
        sas = generate_blob_sas(
            account_name=account,
            container_name=container,
            blob_name=blob_path,
            account_key=key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=max(1, expiry_hours)),
        )
        return f"https://{account}.blob.core.windows.net/{container}/{blob_path}?{sas}"
    except Exception:
        if _url_exists_fast(base_url, blob_rel_path):
            return direct_url
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def _resolve_fighter_image_url_cached(base_url: str, name: str, fighter_id: str) -> str | None:
    """Resolve first existing fighter image URL across folder/file naming variants."""
    file_tokens = _candidate_name_tokens(name)
    for token in _candidate_folder_tokens(name):
        if token not in file_tokens:
            file_tokens.append(token)
    folder_tokens = _candidate_folder_tokens(name)
    if not file_tokens or not folder_tokens:
        return None

    exts = ("webp", "png", "jpg", "jpeg")
    folder_stems: list[str] = []
    file_stems: list[str] = []
    for token in folder_tokens:
        folder_stems.append(f"{token}_{fighter_id}")
        folder_stems.append(f"{token}__{fighter_id}")
    for token in file_tokens:
        file_stems.append(f"{token}_{fighter_id}")
        file_stems.append(f"{token}__{fighter_id}")

    seen: set[str] = set()
    for folder_stem in folder_stems:
        for file_stem in file_stems:
            for ext in exts:
                rel_path = f"{folder_stem}/{file_stem}.{ext}"
                if rel_path in seen:
                    continue
                seen.add(rel_path)
                resolved = _build_blob_read_url(base_url, rel_path)
                if resolved:
                    return resolved

    return None


@st.cache_data(ttl=3600, show_spinner=False)
def _resolve_fighter_image_url_by_name_cached(base_url: str, name: str) -> str | None:
    """Fallback: resolve image by fighter name only when fighter_id variants fail."""
    parsed = _parse_assets_base_url(base_url)
    container_client = _get_assets_container_client(base_url)
    if not parsed or container_client is None:
        return None

    _, _, base_prefix = parsed
    folder_tokens = _candidate_folder_tokens(name)
    if not folder_tokens:
        return None

    preferred_ext = {"webp": 0, "png": 1, "jpg": 2, "jpeg": 3}
    best_blob_name: str | None = None
    best_rank = 999

    for token in folder_tokens:
        for sep in ("_", "__"):
            folder_prefix = f"{token}{sep}"
            starts_with = f"{base_prefix.rstrip('/')}/{folder_prefix}" if base_prefix else folder_prefix
            try:
                for blob_name in container_client.list_blob_names(name_starts_with=starts_with):
                    blob_name_s = str(blob_name)
                    lower = blob_name_s.lower()
                    if not lower.endswith((".webp", ".png", ".jpg", ".jpeg")):
                        continue
                    ext = lower.rsplit(".", 1)[-1]
                    rank = preferred_ext.get(ext, 10)
                    if rank < best_rank:
                        best_rank = rank
                        best_blob_name = blob_name_s
                        if rank == 0:
                            break
                if best_rank == 0:
                    break
            except Exception:
                continue
        if best_rank == 0:
            break

    if not best_blob_name:
        return None

    rel_blob_path = best_blob_name
    if base_prefix and rel_blob_path.startswith(f"{base_prefix.rstrip('/')}/"):
        rel_blob_path = rel_blob_path[len(base_prefix.rstrip('/')) + 1 :]

    return _build_blob_read_url(base_url, rel_blob_path)


@st.cache_data(ttl=3600, show_spinner=False)
def _fighter_name_to_id_map(base: str, prefix: str = "") -> dict[str, str]:
    """Build cached map from fighter display names to fighter_id."""
    df_profiles = _read_parquet(FOLDER_FIGHTER_PROFILES, base, prefix)
    if df_profiles.empty or "fighter_id" not in df_profiles.columns:
        return {}

    name_candidates = [
        c for c in ["fighter_name_display", "fighter_name", "fighter_name_plain"]
        if c in df_profiles.columns
    ]
    if not name_candidates:
        return {}

    mapping: dict[str, str] = {}
    for _, row in df_profiles.iterrows():
        fighter_id = row.get("fighter_id")
        if fighter_id is None or pd.isna(fighter_id):
            continue
        fighter_id_s = str(fighter_id).strip()
        if not fighter_id_s:
            continue
        for col in name_candidates:
            name_val = row.get(col)
            if name_val is None or pd.isna(name_val):
                continue
            name_s = str(name_val).strip()
            if name_s and name_s not in mapping:
                mapping[name_s] = fighter_id_s
    return mapping


def _images_enabled(context: str = "general") -> bool:
    mode = st.session_state.get("image_mode", "smart")
    if mode == "all":
        return True
    if mode == "off":
        return False
    # smart mode: keep heavy table images off; show where visual context matters most
    return context in {"profile", "fight_card", "rankings_table"}


def _get_fighter_id_by_name(name: object) -> str | None:
    """Resolve fighter_id from cached profile map using a display name."""
    if name is None:
        return None
    key = str(name).strip()
    if not key:
        return None
    return _fighter_name_to_id_map(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX).get(key)


def _fighter_image_url(name: object, fighter_id: object | None = None, context: str = "general") -> str | None:
    """Resolve the full fighter image URL from name + fighter_id convention."""
    if not _images_enabled(context):
        return None

    txt = "" if name is None else str(name).strip()
    if not txt or txt in {"Draw", "No Contest", "—"}:
        return None

    resolved_id = fighter_id
    if (resolved_id is None or str(resolved_id).strip() == ""):
        resolved_id = _get_fighter_id_by_name(txt)

    normalized_id = _normalize_fighter_id(resolved_id)
    if normalized_id is None:
        return None

    resolved_id_s = normalized_id
    base_url = _get_fighter_images_base_url()
    resolved = _resolve_fighter_image_url_cached(base_url, txt, resolved_id_s)
    if resolved:
        return resolved

    fallback_folder_tokens = _candidate_folder_tokens(txt)
    fallback_file_tokens = _candidate_name_tokens(txt)
    for token in fallback_folder_tokens:
        if token not in fallback_file_tokens:
            fallback_file_tokens.append(token)

    if not fallback_folder_tokens or not fallback_file_tokens:
        return None

    # Fallback still runs through private-aware URL builder (SAS when needed).
    fallback_rel_candidates: list[str] = []
    for folder_token in fallback_folder_tokens:
        for file_token in fallback_file_tokens:
            fallback_rel_candidates.append(
                f"{folder_token}_{resolved_id_s}/{file_token}_{resolved_id_s}.webp"
            )
            fallback_rel_candidates.append(
                f"{folder_token}__{resolved_id_s}/{file_token}__{resolved_id_s}.webp"
            )

    seen_rel: set[str] = set()
    for rel_path in fallback_rel_candidates:
        if rel_path in seen_rel:
            continue
        seen_rel.add(rel_path)
        resolved_fallback = _build_blob_read_url(base_url, rel_path)
        if resolved_fallback:
            return resolved_fallback

    # Final fallback for id mismatches: discover by fighter-name folder prefix only.
    resolved_name_only = _resolve_fighter_image_url_by_name_cached(base_url, txt)
    if resolved_name_only:
        return resolved_name_only

    return None


def _fighter_image_html(
    name: object,
    fighter_id: object | None = None,
    country: object | None = None,
    size: int = 34,
    context: str = "table",
) -> str:
    """Return a tiny HTML img tag for fighter headshots in HTML tables."""
    url = _fighter_image_url(name, fighter_id, context=context)
    if url:
        safe_alt = escape(str(name) if name is not None else "fighter")
        return (
            f'<img src="{escape(url)}" alt="{safe_alt}" '
            f'style="width:{size}px;height:{size}px;border-radius:50%;object-fit:cover;" '
            'loading="lazy" referrerpolicy="no-referrer" />'
        )

    country_val = "" if country is None else str(country).strip()
    if not country_val:
        txt = "" if name is None else str(name).strip()
        maps = _fighter_country_maps(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
        fid = "" if fighter_id is None else str(fighter_id).strip()
        if fid and fid in maps["by_id"]:
            country_val = maps["by_id"][fid]
        elif txt:
            country_val = maps["by_name"].get(txt, "")

    if not country_val:
        return ""

    if _country_flag_mode() == "cdn":
        flag_img = _country_to_flagcdn_img(country_val, width=size)
        if flag_img:
            return flag_img

    flag_emoji = _country_to_flag(country_val)
    if not flag_emoji:
        return ""
    return (
        f'<span title="{escape(country_val)}" '
        f'style="font-size:{max(16, int(size * 0.85))}px;line-height:1;">{flag_emoji}</span>'
    )


def _fighter_visual_chip_html(
    name: object,
    *,
    fighter_id: object | None = None,
    country: object | None = None,
    size: int = 34,
    context: str = "fight_card",
) -> str:
    return _fighter_image_html(
        name,
        fighter_id=fighter_id,
        country=country,
        size=size,
        context=context,
    )


def _normalize_gender(value: object) -> str:
    txt = "" if value is None else str(value).strip().lower()
    if not txt:
        return ""
    if txt in {"f", "female", "woman", "women", "girl", "w"}:
        return "female"
    if txt in {"m", "male", "man", "men", "boy"}:
        return "male"
    return ""


def _to_boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    if isinstance(value, (int, float)):
        return int(value) == 1
    txt = str(value).strip().lower()
    return txt in {"1", "true", "t", "yes", "y", "champ", "champion"}


def _value_from_candidates(row: pd.Series | dict | None, candidates: list[str]) -> object | None:
    if row is None:
        return None
    for col in candidates:
        if isinstance(row, pd.Series):
            if col in row.index:
                return row.get(col)
        elif isinstance(row, dict):
            if col in row:
                return row.get(col)
    return None


def _resolve_fighter_country(
    name: object,
    fighter_id: object | None = None,
    country: object | None = None,
) -> str:
    country_val = "" if country is None else str(country).strip()
    if country_val:
        return _canonical_country_name(country_val)

    maps = _fighter_country_maps(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    fid = "" if fighter_id is None else str(fighter_id).strip()
    if fid and fid in maps["by_id"]:
        return maps["by_id"][fid]

    txt = "" if name is None else str(name).strip()
    if txt:
        variants: list[str] = []
        for candidate in (txt, txt.casefold()):
            c = str(candidate).strip()
            if c and c not in variants:
                variants.append(c)
        no_parens = re.sub(r"\s*\([^)]*\)", "", txt).strip()
        for candidate in (no_parens, no_parens.casefold()):
            c = str(candidate).strip()
            if c and c not in variants:
                variants.append(c)
        normalized = _normalize_fighter_name_key(txt)
        normalized_no_parens = _normalize_fighter_name_key(no_parens)
        for candidate in (normalized, normalized_no_parens):
            c = str(candidate).strip()
            if c and c not in variants:
                variants.append(c)

        for candidate in variants:
            found = maps["by_name"].get(candidate, "")
            if found:
                return _canonical_country_name(found)
    return ""


def _normalize_fighter_name_key(name: object) -> str:
    txt = "" if name is None else str(name).strip()
    if not txt:
        return ""
    txt = re.sub(r"\s*\([^)]*\)", "", txt).strip()
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    txt = txt.casefold()
    txt = re.sub(r"[^a-z0-9\s\-_]", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _gender_from_weight_class(weight_class: object) -> str:
    txt = "" if weight_class is None else str(weight_class).strip().lower()
    return "female" if "women" in txt else ""


@st.cache_data(ttl=3600, show_spinner=False)
def _fighter_identity_map(base: str, prefix: str = "") -> dict[str, dict[str, object]]:
    """Best-effort identity attributes (gender/champion) keyed by fighter name."""
    df_profiles = _read_parquet(FOLDER_FIGHTER_PROFILES, base, prefix)
    if df_profiles.empty:
        return {}

    name_cols = [
        c for c in ["fighter_name_display", "fighter_name", "fighter_name_plain"]
        if c in df_profiles.columns
    ]
    if not name_cols:
        return {}

    gender_cols = [
        c for c in ["gender", "sex", "fighter_gender", "fighter_sex"] if c in df_profiles.columns
    ]
    champion_cols = [
        c
        for c in [
            "is_champion",
            "champion",
            "title_holder",
            "is_title_holder",
            "champion_status",
        ]
        if c in df_profiles.columns
    ]

    out: dict[str, dict[str, object]] = {}
    for _, row in df_profiles.iterrows():
        gender_val = ""
        for gc in gender_cols:
            g = _normalize_gender(row.get(gc))
            if g:
                gender_val = g
                break
        if not gender_val:
            gender_val = _gender_from_weight_class(row.get("weight_class"))

        champ_val = False
        for cc in champion_cols:
            if _to_boolish(row.get(cc)):
                champ_val = True
                break

        for nc in name_cols:
            name = row.get(nc)
            if name is None or pd.isna(name):
                continue
            name_s = str(name).strip()
            if not name_s:
                continue
            if name_s not in out:
                out[name_s] = {
                    "gender": gender_val,
                    "is_champion": champ_val,
                }
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _fighter_card_stats_map(base: str, prefix: str = "") -> dict[str, dict[str, object]]:
    """Card stat payloads keyed by normalized fighter names."""
    df_profiles = _read_parquet(FOLDER_FIGHTER_PROFILES, base, prefix)
    if df_profiles.empty:
        return {}

    name_cols = [
        c for c in ["fighter_name_display", "fighter_name", "fighter_name_plain"]
        if c in df_profiles.columns
    ]
    if not name_cols:
        return {}

    def _pick(row: pd.Series, candidates: list[str]) -> object | None:
        return _value_from_candidates(row, candidates)

    def _present(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, float) and pd.isna(value):
            return False
        txt = str(value).strip()
        return txt.lower() not in {"", "nan", "nat", "none"}

    def _score(payload: dict[str, object]) -> int:
        return sum(1 for value in payload.values() if _present(value))

    out: dict[str, dict[str, object]] = {}
    for _, row in df_profiles.iterrows():
        payload = {
            "country": _pick(row, ["country", "fighter_country", "country_name"]),
            "weight_class": _pick(row, ["weight_class", "fighter_weight_class", "division"]),
            "finish_rate": _pick(row, ["finish_rate_win_shrunk", "finish_rate"]),
            "sub_rate": _pick(row, ["sub_rate_win_shrunk", "sub_rate"]),
            "win_streak": _pick(row, ["win_streak", "fighter_win_streak"]),
            "loss_streak": _pick(row, ["loss_streak", "fighter_loss_streak"]),
            "wins": _pick(row, ["wins", "wins_count", "fighter_wins"]),
            "losses": _pick(row, ["losses", "losses_count", "fighter_losses"]),
        }
        payload_score = _score(payload)
        if payload_score == 0:
            continue
        for col in name_cols:
            key = _normalize_fighter_name_key(row.get(col))
            if not key:
                continue
            existing = out.get(key)
            if existing is None or payload_score >= _score(existing):
                out[key] = payload
    return out


def _iso2_to_flag(code: str) -> str:
    txt = (code or "").strip().upper()
    if len(txt) != 2 or not txt.isalpha():
        return ""
    return chr(ord(txt[0]) + 127397) + chr(ord(txt[1]) + 127397)


def _country_to_iso2(country: object) -> str:
    return _shared_country_iso2(country)


def _country_to_flag(country: object) -> str:
    return _shared_country_flag(country)


def _country_flag_mode() -> str:
    mode = os.environ.get("FRONT_COUNTRY_FLAG_MODE", "cdn").strip().lower()
    return mode if mode in {"cdn", "emoji"} else "cdn"


def _country_to_flagcdn_img(country: object, *, width: int = 20) -> str:
    code = _country_to_iso2(country).lower()
    if not code:
        return ""
    raw_country = "" if country is None else str(country).strip()
    title = escape(raw_country or code.upper())
    return (
        f'<img src="https://flagcdn.com/w40/{code}.png" '
        f'alt="{title}" title="{title}" '
        'onerror="this.style.display=\'none\';" '
        f'width="{int(width)}" loading="lazy" referrerpolicy="no-referrer" '
        'style="vertical-align:-2px;border-radius:2px;box-shadow:0 0 0 1px rgba(255,255,255,0.18);"/>'
    )


def _country_inline_html(
    country: object,
    *,
    na_text: str = "N/A",
    width: int = 24,
    prefer_cdn: bool = True,
    include_label: bool = False,
) -> str:
    raw_country = _canonical_country_name(country)
    if not raw_country:
        if include_label:
            return (
                "<span style='display:inline-flex;align-items:center;gap:0.35rem;color:#a1a1aa;'>"
                f"<span>Country: {escape(na_text)}</span></span>"
            )
        return (
            "<span style='display:inline-flex;align-items:center;gap:0.35rem;color:#a1a1aa;'>"
            f"<span>{escape(na_text)}</span></span>"
        )

    flag_html = ""
    if prefer_cdn or _country_flag_mode() == "cdn":
        flag_html = _country_to_flagcdn_img(raw_country, width=width)
    if not flag_html:
        flag_emoji = _country_to_flag(raw_country)
        if flag_emoji:
            flag_html = f"<span style='font-size:1rem;line-height:1;'>{flag_emoji}</span>"

    if include_label:
        text_html = f"<span>Country: {escape(raw_country)}</span>"
    else:
        text_html = f"<span>{escape(raw_country)}</span>"

    if flag_html:
        return (
            "<span style='display:inline-flex;align-items:center;gap:0.35rem;color:#d4d4d8;'>"
            f"{flag_html}{text_html}</span>"
        )

    return f"<span style='color:#d4d4d8;'>{escape(raw_country)}</span>"


def _country_display_html(
    country: object,
    *,
    na_text: str = "N/A",
    width: int = 18,
    prefer_cdn: bool = False,
) -> str:
    raw_country = _canonical_country_name(country)
    if not raw_country:
        return f"<span style='color:#a1a1aa;'>Country: {escape(na_text)}</span>"

    flag_html = ""
    if prefer_cdn or _country_flag_mode() == "cdn":
        flag_html = _country_to_flagcdn_img(raw_country, width=width)
    if not flag_html:
        flag_emoji = _country_to_flag(raw_country)
        if flag_emoji:
            flag_html = f"<span style='font-size:1rem;line-height:1;'>{flag_emoji}</span>"

    if flag_html:
        return (
            "<span style='display:inline-flex;align-items:center;gap:0.35rem;color:#d4d4d8;'>"
            f"{flag_html}<span>Country: {escape(raw_country)}</span></span>"
        )

    return f"<span style='color:#d4d4d8;'>Country: {escape(raw_country)}</span>"


@st.cache_data(ttl=3600, show_spinner=False)
def _fighter_country_maps(base: str, prefix: str = "") -> dict[str, dict[str, str]]:
    try:
        df = _read_parquet(FOLDER_FIGHTER_PROFILES, base, prefix)
    except Exception:
        return {"by_id": {}, "by_name": {}}
    if df.empty or "country" not in df.columns:
        return {"by_id": {}, "by_name": {}}

    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}

    id_col = "fighter_id" if "fighter_id" in df.columns else None
    name_cols = [c for c in ["fighter_name_display", "fighter_name", "fighter_name_plain"] if c in df.columns]

    for _, row in df.iterrows():
        country = _canonical_country_name(row.get("country"))
        if not country:
            continue
        if id_col:
            fid = str(row.get(id_col, "") or "").strip()
            if fid and fid not in by_id:
                by_id[fid] = country
        for nc in name_cols:
            name = str(row.get(nc, "") or "").strip()
            if not name:
                continue
            variants: list[str] = [name, name.casefold()]
            name_no_parens = re.sub(r"\s*\([^)]*\)", "", name).strip()
            if name_no_parens:
                variants.extend([name_no_parens, name_no_parens.casefold()])
            name_norm = _normalize_fighter_name_key(name)
            if name_norm:
                variants.append(name_norm)
            name_no_parens_norm = _normalize_fighter_name_key(name_no_parens)
            if name_no_parens_norm:
                variants.append(name_no_parens_norm)
            for v in variants:
                key = str(v).strip()
                if key and key not in by_name:
                    by_name[key] = country

    return {"by_id": by_id, "by_name": by_name}


def _fighter_badge(
    name: object,
    *,
    fighter_id: object | None = None,
    country: object | None = None,
    gender: object = None,
    is_champion: object = None,
) -> str:
    txt = "" if name is None else str(name).strip()
    if not txt or txt in {"Draw", "No Contest", "—"}:
        return ""

    g = _normalize_gender(gender)
    if not g:
        ident = _fighter_identity_map(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX).get(txt, {})
        g = _normalize_gender(ident.get("gender"))
        if is_champion is None:
            is_champion = ident.get("is_champion")

    country_val = "" if country is None else str(country).strip()
    if not country_val:
        maps = _fighter_country_maps(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
        fid = "" if fighter_id is None else str(fighter_id).strip()
        if fid and fid in maps["by_id"]:
            country_val = maps["by_id"][fid]
        else:
            country_val = maps["by_name"].get(txt, "")

    flag = _country_to_flag(country_val)
    belt = _goat_icon_html(size=13, extra_class="fp-inline-goat--champ") if _to_boolish(is_champion) else ""
    return f"{flag} {belt}".strip() if flag else belt


def _fighter_badge_from_row(row: pd.Series, side: str) -> str:
    side = side.strip().lower()
    if side not in {"fighter", "opponent"}:
        side = "fighter"

    name = row.get(f"{side}_name_display")
    gender_candidates = [
        f"{side}_gender",
        f"{side}_sex",
        f"{side}_gender_label",
        "gender",
        "sex",
    ]
    champ_candidates = [
        f"{side}_is_champion",
        f"{side}_champion",
        f"{side}_is_title_holder",
        f"{side}_title_holder",
        "is_champion",
        "champion",
    ]
    country_candidates = [
        f"{side}_country",
        f"{side}_country_code",
        "country",
        "country_code",
    ]
    fighter_id_candidates = [
        f"{side}_fighter_id",
        f"{side}_id",
        f"{side}_fighterid",
        "fighter_id",
    ]

    gender_val = _value_from_candidates(row, gender_candidates)
    champ_val = _value_from_candidates(row, champ_candidates)
    country_val = _value_from_candidates(row, country_candidates)
    fighter_id_val = _value_from_candidates(row, fighter_id_candidates)

    if not _normalize_gender(gender_val):
        wc = row.get("weight_class")
        inferred = _gender_from_weight_class(wc)
        if inferred:
            gender_val = inferred

    return _fighter_badge(
        name,
        fighter_id=fighter_id_val,
        country=country_val,
        gender=gender_val,
        is_champion=champ_val,
    )


@st.cache_data(ttl=3600, show_spinner=False)
def _title_lineage_maps(base: str, prefix: str = "") -> dict[str, object]:
    """Compute current/former champion maps using belt-lineage tracking.

    The ETL sometimes marks *every* fight on a card as ``is_title_fight``
    when only the main/co-main really are championship bouts.  A naïve
    "latest title-fight winner per weight class" therefore picks the wrong
    person.

    This function instead replays the title-fight timeline **chronologically**
    for each division and only recognises a bout as a real championship fight
    when:
      • the belt is *vacant* (no champion yet in that division), **or**
      • the reigning champion is one of the two fighters.

    Any row flagged ``is_title_fight`` that fails the check is silently
    skipped (bad/noisy data).
    """
    df_hist = _read_parquet(FOLDER_FIGHTER_HISTORY, base, prefix)
    empty: dict[str, object] = {
        "current_ids": set(),
        "current_names": set(),
        "current_classes_by_id": {},
        "current_classes_by_name": {},
        "former_ids": set(),
        "former_names": set(),
    }
    if df_hist.empty:
        return empty

    # ---- helpers --------------------------------------------------------
    def _is_title(v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return False
        txt = str(v).strip().lower()
        if txt in {"1", "true", "t", "yes", "y", "title", "title fight", "title_fight"}:
            return True
        return "title" in txt and txt not in {"", "none", "nan", "nat"}

    title_mask = (
        df_hist["is_title_fight"].apply(_is_title)
        if "is_title_fight" in df_hist.columns
        else pd.Series(False, index=df_hist.index)
    )
    if not bool(title_mask.any()):
        return empty

    title_df = df_hist[title_mask].copy()
    if "event_date" not in title_df.columns or "weight_class" not in title_df.columns:
        return empty

    title_df["event_date"] = pd.to_datetime(title_df["event_date"], errors="coerce")
    title_df["weight_class_norm"] = title_df["weight_class"].astype(str).str.strip()
    title_df = title_df[title_df["event_date"].notna() & (title_df["weight_class_norm"] != "")].copy()
    if title_df.empty:
        return empty

    fighter_name_col = (
        "fighter_name_display" if "fighter_name_display" in title_df.columns
        else ("fighter_name" if "fighter_name" in title_df.columns else None)
    )
    if fighter_name_col is None:
        return empty

    has_result = "result" in title_df.columns
    has_winner = "winner_name_display" in title_df.columns
    has_opponent = "opponent_name_display" in title_df.columns
    has_fid = "fighter_id" in title_df.columns
    has_oid = "opponent_id" in title_df.columns

    # ---- deduplicate to one row per fight (winner row) ------------------
    title_df["_fighter"] = title_df[fighter_name_col].astype(str).str.strip()
    title_df["_opponent"] = (
        title_df["opponent_name_display"].astype(str).str.strip() if has_opponent
        else pd.Series("", index=title_df.index)
    )
    title_df["_result"] = (
        title_df["result"].astype(str).str.strip().str.lower() if has_result
        else pd.Series("", index=title_df.index)
    )
    title_df["_winner"] = (
        title_df["winner_name_display"].astype(str).str.strip() if has_winner
        else pd.Series("", index=title_df.index)
    )

    win_mask = title_df["_result"].isin({"win", "w", "winner"}) | (
        (title_df["_winner"] != "") & (title_df["_winner"] == title_df["_fighter"])
    )
    win_rows = title_df[win_mask].copy()
    if win_rows.empty:
        return empty

    # ---- belt-lineage tracking per division (chronological) -------------
    # current_champ[wc] = name of reigning champ (or None if vacant)
    current_champ: dict[str, str | None] = {}
    # last date the reigning champ participated in a title fight
    champ_last_title_date: dict[str, pd.Timestamp] = {}
    # If the reigning champion hasn't appeared in a title fight for this
    # long, treat the belt as vacant (handles broken chains in old data).
    _MAX_STALENESS = pd.Timedelta(days=3 * 365)
    # all fighters who ever legitimately won a title fight
    all_title_winners_names: set[str] = set()
    all_title_winners_ids: set[str] = set()
    # final current champion info
    current_ids: set[str] = set()
    current_names: set[str] = set()
    current_classes_by_id: dict[str, set[str]] = {}
    current_classes_by_name: dict[str, set[str]] = {}

    # Sort ALL winner rows chronologically
    win_rows = win_rows.sort_values("event_date", ascending=True)

    for _, row in win_rows.iterrows():
        wc = str(row.get("weight_class_norm", "") or "").strip()
        winner = str(row.get("_fighter", "") or "").strip()
        opponent = str(row.get("_opponent", "") or "").strip()
        fight_date = row.get("event_date")
        if not wc or not winner:
            continue

        reigning = current_champ.get(wc)

        # Staleness check: if the reigning champion hasn't been in a title
        # fight for > 3 years, consider the belt effectively vacant.
        if reigning is not None:
            last_dt = champ_last_title_date.get(wc)
            if last_dt is not None and pd.notna(fight_date) and (fight_date - last_dt) > _MAX_STALENESS:
                current_champ[wc] = None
                reigning = None

        if reigning is None:
            # Belt is vacant → accept this as the new championship fight
            pass
        elif reigning == winner or reigning == opponent:
            # Reigning champ is one of the two fighters: legitimate
            pass
        else:
            # Neither fighter is the champ → bad data / not a real title bout
            continue

        # This is a legitimate championship fight — winner takes the belt
        current_champ[wc] = winner
        if pd.notna(fight_date):
            champ_last_title_date[wc] = fight_date
        all_title_winners_names.add(winner)
        if has_fid:
            fid = str(row.get("fighter_id", "") or "").strip()
            if fid:
                all_title_winners_ids.add(fid)

    # ---- build current vs former sets ----------------------------------
    for wc, champ_name in current_champ.items():
        if not champ_name:
            continue
        current_names.add(champ_name)
        current_classes_by_name.setdefault(champ_name, set()).add(wc)

    # Resolve current champion IDs from the win_rows data
    if has_fid and current_names:
        id_lookup = (
            win_rows[win_rows["_fighter"].isin(current_names)]
            .groupby("_fighter")
            .last()  # latest row per fighter
        )
        for name in current_names:
            if name in id_lookup.index:
                fid = str(id_lookup.loc[name].get("fighter_id", "") or "").strip()
                if fid:
                    current_ids.add(fid)
                    wcs = current_classes_by_name.get(name, set())
                    for w in wcs:
                        current_classes_by_id.setdefault(fid, set()).add(w)

    former_ids = all_title_winners_ids - current_ids
    former_names = all_title_winners_names - current_names

    return {
        "current_ids": current_ids,
        "current_names": current_names,
        "current_classes_by_id": current_classes_by_id,
        "current_classes_by_name": current_classes_by_name,
        "former_ids": former_ids,
        "former_names": former_names,
    }


def _get_static_logo_paths() -> list[Path]:
    """Return all local logo/image files from the project static folder."""
    if not _STATIC_DIR.exists():
        return []
    allowed_ext = {".png", ".jpg", ".jpeg", ".webp"}
    return sorted(
        [
            p
            for p in _STATIC_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in allowed_ext
        ],
        key=lambda path: path.name.lower(),
    )


def _favicon_data_uri(path: Path | None) -> str:
    if path is None or not path.exists() or not path.is_file():
        return ""
    ext = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".svg": "image/svg+xml",
    }.get(ext, "image/png")
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return ""
    return f"data:{mime};base64,{payload}"


def _get_goat_icon_path() -> Path | None:
    if _GOAT_EMOJI_PNG_PATH.exists() and _GOAT_EMOJI_PNG_PATH.is_file():
        return _GOAT_EMOJI_PNG_PATH
    return None


@st.cache_data(show_spinner=False)
def _goat_icon_data_uri() -> str:
    return _favicon_data_uri(_get_goat_icon_path())


@st.cache_data(show_spinner=False)
def _named_icon_data_uri(filename: str) -> str:
    if not filename:
        return ""
    path = _STATIC_DIR / filename
    return _favicon_data_uri(path if path.exists() and path.is_file() else None)


def _png_icon_html(
    filename: str,
    *,
    size: int = 16,
    extra_class: str = "",
    label: str = "",
) -> str:
    icon_uri = _named_icon_data_uri(filename)
    classes = "fp-inline-goat"
    if extra_class:
        classes = f"{classes} {extra_class}"
    if icon_uri:
        safe_label = escape(label)
        aria_hidden = "false" if label else "true"
        aria_label_attr = f' aria-label="{safe_label}" role="img"' if label else ""
        return (
            f'<span aria-hidden="{aria_hidden}"{aria_label_attr} class="{classes}" '
            f'style="width:{size}px;height:{size}px;'
            f"background-image:url('{icon_uri}');"
            'background-position:center;background-repeat:no-repeat;'
            'background-size:contain;"></span>'
        )
    return ""


def _goat_icon_html(*, size: int = 16, extra_class: str = "", label: str = "") -> str:
    icon_html = _png_icon_html(
        "b91c1c-goat-emoji-rail.png",
        size=size,
        extra_class=extra_class,
        label=label,
    )
    if icon_html:
        return icon_html
    classes = "fp-inline-goat"
    if extra_class:
        classes = f"{classes} {extra_class}"
    fallback = escape(label or "GOAT")
    return (
        f'<span class="{classes} fp-inline-goat--fallback" '
        f'style="font-size:{size}px;line-height:1;">{fallback}</span>'
    )


def _nav_icon_html(slug: str, *, size: int = 16, label: str = "") -> str:
    filename = _NAV_ICON_FILES.get((slug or "").strip().lower(), "b91c1c-goat-emoji.png")
    icon_html = _png_icon_html(filename, size=size, label=label)
    return icon_html or _goat_icon_html(size=size, label=label)


def _nav_icon_class(slug: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (slug or "").strip().lower()).strip("-")
    return normalized or "default"


def _icon_label_html(label: str, *, size: int = 16, wrapper_class: str = "fp-icon-label") -> str:
    return (
        f'<span class="{wrapper_class}">'
        f"{_goat_icon_html(size=size)}"
        f"<span>{escape(label)}</span>"
        "</span>"
    )


def _icon_markup(value: str | None, *, default_size: int = 16) -> str:
    if value is None:
        return _goat_icon_html(size=default_size)
    text = str(value)
    stripped = text.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return text
    return escape(text)


def _get_favicon_icon_paths() -> tuple[Path | None, Path | None]:
    """Return (light_theme_icon, dark_theme_icon) with best-effort fallbacks.

    light_theme_icon: used when browser/OS theme is light (prefer black icon)
    dark_theme_icon: used when browser/OS theme is dark (prefer white icon)
    """
    light_candidates = [
        _STATIC_DIR / "ear_favicon_transparent.png",
        _STATIC_DIR / "ear_favicon_transparent.ico",
        _STATIC_DIR / "ear_icon_transparent.png",
        _STATIC_DIR / "fightprophet_ear_black.png",
    ]
    dark_candidates = [
        _STATIC_DIR / "white_ear_favicon.png",
        _STATIC_DIR / "white_ear_favicon.ico",
        _STATIC_DIR / "fightprophet_ear_white.png",
        _STATIC_DIR / "ear_favicon_transparent.png",
    ]

    light_icon = next((p for p in light_candidates if p.exists()), None)
    dark_icon = next((p for p in dark_candidates if p.exists()), None)
    return light_icon, dark_icon


def _get_default_page_icon() -> str:
    """Default favicon used by Streamlit page config before JS theme swap runs."""
    light_icon, dark_icon = _get_favicon_icon_paths()
    chosen = light_icon or dark_icon
    if chosen is not None and chosen.exists():
        return str(chosen)
    return "Fight Prophet"


def _inject_theme_aware_favicon() -> None:
    """Swap favicon by browser theme and patch icon-font ligature fallbacks."""
    light_icon, dark_icon = _get_favicon_icon_paths()
    light_uri = _favicon_data_uri(light_icon)
    dark_uri = _favicon_data_uri(dark_icon)
    if not light_uri and not dark_uri:
        return

    if not light_uri:
        light_uri = dark_uri
    if not dark_uri:
        dark_uri = light_uri

    st.markdown(
        f"""
<script>
(function() {{
  const lightIcon = {json.dumps(light_uri)};
  const darkIcon = {json.dumps(dark_uri)};
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const ligatureToken = /^(keyboard_)?double_arrow_(left|right)$|^(keyboard_)?arrow_(left|right|forward|back|forward_ios|back_ios)$|^chevron_(left|right)$|^expand_(more|less)$|^arrow_drop_(down|up)$|^unfold_(more|less)$|^menu$/;
    const ligatureSubstring = ['keyboard_double', 'double_arrow', 'arrow_right', 'arrow_left', 'arrow_forward', 'expand_more', 'expand_less', 'arrow_drop'];

    function looksLikeArrowLigature(text) {{
        if (!text) return false;
        const normalized = String(text).trim().toLowerCase().replace(/\\s+/g, '_');
        if (!normalized) return false;
        for (const sub of ligatureSubstring) {{
            if (normalized === sub || normalized.endsWith('_' + sub) || normalized.startsWith(sub + '_') || normalized === sub) return true;
        }}
        return ligatureToken.test(normalized);
    }}

    function patchToggleEl(toggle) {{
        if (!toggle) return;
        toggle.classList.add('fp-toggle-patched');
        toggle.setAttribute('aria-label', 'Toggle sidebar');
        toggle.querySelectorAll('.fp-toggle-icon').forEach((icon) => icon.remove());
    }}

    function getOwnText(el) {{
        let own = '';
        for (const node of el.childNodes) {{
            if (node.nodeType === 3) own += node.textContent;
        }}
        return own.trim().toLowerCase();
    }}

    function hideLigatureFallbacks() {{
        document.querySelectorAll('span, i, div, button').forEach((el) => {{
            const ownTxt = getOwnText(el);
            const fullTxt = (el.textContent || '').trim().toLowerCase();
            /* If the element's OWN text (not children) is a ligature, clear it */
            if (ownTxt && looksLikeArrowLigature(ownTxt)) {{
                for (const node of [...el.childNodes]) {{
                    if (node.nodeType === 3 && looksLikeArrowLigature(node.textContent.trim().toLowerCase())) {{
                        node.textContent = '';
                    }}
                }}
                el.style.fontSize = el.children.length ? '' : '0';
                el.style.lineHeight = el.children.length ? '' : '0';
            }}
            /* If the ENTIRE element text is just a ligature (leaf node), hide it */
            else if (!el.children.length && looksLikeArrowLigature(fullTxt)) {{
                el.textContent = '';
                el.style.fontSize = '0';
                el.style.lineHeight = '0';
            }}
        }});

        const toggleSelectors = [
            '[data-testid="collapsedControl"] button',
            '[data-testid="stSidebarCollapseButton"] button',
            '[data-testid="stSidebarCollapsedControl"] button',
            '[data-testid="stExpandSidebarButton"] button',
            '[data-testid="stSidebarHeader"] button',
        ];
        const toggleInsideSelector = toggleSelectors.join(', ');

        const attrNodes = [
            ...document.querySelectorAll(toggleInsideSelector),
            ...document.querySelectorAll(toggleSelectors.map((s) => s + ' *').join(', ')),
            ...document.querySelectorAll('[title*="keyboard_double" i], [aria-label*="keyboard_double" i]'),
            ...document.querySelectorAll('[title*="double_arrow" i], [aria-label*="double_arrow" i]'),
        ];

        attrNodes.forEach((el) => {{
            const title = el.getAttribute && el.getAttribute('title');
            const label = el.getAttribute && el.getAttribute('aria-label');

            if (title && looksLikeArrowLigature(title)) {{
                el.removeAttribute('title');
            }}

            if (label && looksLikeArrowLigature(label)) {{
                if (el.closest(toggleInsideSelector)) {{
                    el.setAttribute('aria-label', 'Toggle sidebar');
                }} else {{
                    el.removeAttribute('aria-label');
                }}
            }}
        }});

        const toggles = [
            ...document.querySelectorAll(toggleInsideSelector),
            ...document.querySelectorAll(toggleSelectors.map((s) => s + ' button').join(', ')),
            ...document.querySelectorAll('button[aria-label*="sidebar" i]'),
            ...document.querySelectorAll('button[title*="sidebar" i]'),
        ];
        toggles.forEach((toggle) => patchToggleEl(toggle));
    }}

  function setFavicon() {{
    const href = media.matches ? darkIcon : lightIcon;
    if (!href) return;
    let link = document.querySelector("link[rel='icon']") || document.querySelector("link[rel='shortcut icon']");
    if (!link) {{
      link = document.createElement('link');
      link.setAttribute('rel', 'icon');
      document.head.appendChild(link);
    }}
    link.setAttribute('type', 'image/png');
    link.setAttribute('href', href);
  }}

  setFavicon();
    hideLigatureFallbacks();
    const obs = new MutationObserver(() => hideLigatureFallbacks());
    obs.observe(document.body, {{ childList: true, subtree: true }});
  if (media.addEventListener) {{
    media.addEventListener('change', setFavicon);
  }} else if (media.addListener) {{
    media.addListener(setFavicon);
  }}
}})();
</script>
""",
        unsafe_allow_html=True,
    )


def _get_sidebar_logo_slots() -> tuple[Path | None, Path | None]:
    """Pick primary and secondary sidebar logos from static assets."""
    logo_paths = _get_static_logo_paths()
    logo_paths = [
        p for p in logo_paths
        if "favicon" not in p.name.lower() and "cageicon" not in p.name.lower()
    ]
    if not logo_paths:
        return None, None

    preferred_primary = _STATIC_DIR / "fightprophet_logo_white.png"
    preferred_secondary = _STATIC_DIR / "fightprophet_ear_white.png"

    if preferred_primary.exists():
        primary = preferred_primary
    else:
        primary = logo_paths[0]

    if preferred_secondary.exists() and preferred_secondary != primary:
        secondary = preferred_secondary
    else:
        remaining = [p for p in logo_paths if p != primary and "ear" in p.name.lower()]
        if not remaining:
            remaining = [p for p in logo_paths if p != primary]
        secondary = remaining[0] if remaining else None
    return primary, secondary


def _read_image_bytes(path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        if not path.exists() or not path.is_file():
            return None
        payload = path.read_bytes()
        if not payload:
            return None
        if not _is_supported_image_bytes(payload):
            return None
        return payload
    except Exception:
        return None


def _is_supported_image_bytes(payload: bytes) -> bool:
    if not payload:
        return False

    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if payload.startswith(b"\xff\xd8\xff"):
        return True
    if payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        return True
    if payload.startswith(b"RIFF") and len(payload) >= 12 and payload[8:12] == b"WEBP":
        return True
    return False


def _get_home_header_logo() -> Path | None:
    """Prefer the white ear logo for the home header."""
    preferred = _STATIC_DIR / "fightprophet_ear_white.png"
    if preferred.exists():
        return preferred

    for logo_path in _get_static_logo_paths():
        if "ear" in logo_path.name.lower():
            return logo_path
    return None


def _get_sidebar_contact_logo() -> Path | None:
    """Pick a small ear logo specifically for the Contact section."""
    candidates = [
        _STATIC_DIR / "fightprophet_ear_white.png",
        _STATIC_DIR / "fightprophet_ear_black.png",
        _STATIC_DIR / "white_ear_favicon.png",
        _STATIC_DIR / "ear_favicon_transparent.png",
        _STATIC_DIR / "ear_icon_transparent.png",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _render_sidebar_primary_logo() -> None:
    primary, _ = _get_sidebar_logo_slots()
    img_bytes = _read_image_bytes(primary)
    if img_bytes is not None:
        st.image(img_bytes, width="stretch")


def _render_sidebar_footer_logo() -> None:
    secondary = _get_sidebar_contact_logo()
    if secondary is None:
        _, secondary = _get_sidebar_logo_slots()
    if secondary is None:
        _, dark_icon = _get_favicon_icon_paths()
        secondary = dark_icon
    st.markdown("<div style='height: 0.35rem;'></div>", unsafe_allow_html=True)
    secondary_bytes = _read_image_bytes(secondary)
    if secondary_bytes is not None:
        import base64 as _b64
        ear_b64 = _b64.b64encode(secondary_bytes).decode()
        st.markdown(
            f"""
            <div style="display:flex; justify-content:center; align-items:center; padding:0.2rem 0 0.1rem;">
                <img src="data:image/png;base64,{ear_b64}" alt="Fight Prophet"
                     style="width:42px; height:auto; object-fit:contain; opacity:0.94;
                            filter:drop-shadow(0 0 8px rgba(220,38,38,0.35));" />
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown("<div style='height:0.15rem;'></div>", unsafe_allow_html=True)
    st.markdown(
        """
        <div style="text-align:center;">
            <div style="font-size:1.02rem; font-weight:700; margin-top:0.15rem; margin-bottom:0.45rem;">Contact</div>
            <div style="font-size:0.95rem; line-height:1.5;">
                <a href="https://www.linkedin.com/company/fight-prophet" target="_blank">Business LinkedIn</a><br/>
                <a href="https://www.linkedin.com/in/datatomas/" target="_blank">Founder LinkedIn</a><br/>
                <a href="https://github.com/datatomas/fight_prophet" target="_blank">Business GitHub</a><br/>
                <a href="https://medium.com/@datatomas" target="_blank">Medium</a><br/>
                <a href="mailto:datatomas@uppercutanalytics.com">Email</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _azure_sidebar_defaults() -> tuple[str, str]:
    """Return fixed Azure base + prefix for production sidebar usage."""
    azure_base = os.environ.get("AZURE_PARQUET_BASE_URI", "").strip()
    if azure_base:
        base = azure_base.rstrip("/")
    else:
        container = os.environ.get("AZURE_STORAGE_CONTAINER", "fightprophet-dashboard").strip()
        base = f"az://{container}"

    prefix = _get_parquet_prefix().strip("/")
    return base, prefix


def _show_azure_test_controls() -> bool:
    """Show Azure test tools only during testing/debug sessions."""
    raw = os.environ.get("FRONT_ENABLE_AZURE_TEST", "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _get_ear_logo_nudge_px() -> int:
    """Small optical horizontal nudge for ear logo centering (+ right, - left)."""
    raw = os.environ.get("FRONT_EAR_LOGO_NUDGE_PX", "6").strip()
    try:
        val = int(raw)
    except Exception:
        val = 6
    return max(-30, min(30, val))


# ---------------------------------------------------------------------------
# Parquet reader (in-process DuckDB, read-only, no file lock)
# ---------------------------------------------------------------------------

@st.cache_resource
def _reader(base: str) -> duckdb.DuckDBPyConnection:
    """Ephemeral in-memory DuckDB used only as a Parquet reader.

    When PARQUET_BASE_URI starts with ``az://``, the Azure extension is
    loaded and credentials are configured from env vars so that
    ``read_parquet('az://...')`` works transparently.
    """
    conn = duckdb.connect(":memory:", read_only=False)
    if _is_azure(base):
        account = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
        key = os.environ.get("AZURE_STORAGE_KEY", "")
        if account and key:
            conn.execute("INSTALL azure; LOAD azure;")
            conn_str = (
                f"DefaultEndpointsProtocol=https;"
                f"AccountName={account};"
                f"AccountKey={key};"
                f"EndpointSuffix=core.windows.net"
            )
            conn.execute(
                f"CREATE SECRET azure_read (TYPE AZURE, CONNECTION_STRING '{conn_str}')"
            )
        else:
            logger.warning(
                "PARQUET_BASE_URI is an Azure URI but AZURE_STORAGE_ACCOUNT "
                "and/or AZURE_STORAGE_KEY are not set."
            )
    return conn


def _resolve_latest(base: str, folder: str) -> str | None:
    """Read LATEST.json for the freshest export path.

    For Azure URIs the LATEST.json is fetched via the Blob SDK so
    the reader container does not need the DuckDB Azure extension
    just for a tiny JSON file.  Falls back to a wildcard glob if
    LATEST.json is missing.
    """
    if _is_azure(base):
        return _resolve_latest_azure(base, folder)

    latest_file = Path(f"{base}/{folder}/LATEST.json")
    if latest_file.exists():
        try:
            meta = json.loads(latest_file.read_text())
            path = meta.get("path", "")
            if path:
                return path
        except Exception:
            pass
    # Fallback: glob all versions
    fallback = f"{base}/{folder}/**/data.parquet"
    if Path(f"{base}/{folder}").exists():
        return fallback
    return None


@st.cache_data(ttl=_get_front_cache_ttl_seconds(), show_spinner=False)
def _resolve_latest_azure(base: str, folder: str) -> str | None:
    """Fetch LATEST.json from Azure Blob Storage (cached)."""
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
    key = os.environ.get("AZURE_STORAGE_KEY", "")
    # Derive container from the az://container URI
    container = base.replace("az://", "").replace("azure://", "").split("/")[0]
    blob_name = f"{folder}/LATEST.json"

    if not account or not key:
        # Fall back to a glob that DuckDB Azure extension can resolve
        return f"{base}/{folder}/**/*.parquet"

    try:
        from azure.storage.blob import BlobServiceClient

        client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
        blob = client.get_blob_client(container=container, blob=blob_name)
        data = blob.download_blob().readall()
        meta = json.loads(data)
        path = meta.get("path", "")
        if path:
            return path
    except Exception:
        pass

    return f"{base}/{folder}/**/*.parquet"


def _azure_blob_client(base: str):
    """Build Azure BlobServiceClient + container name from az:// base URI."""
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
    key = os.environ.get("AZURE_STORAGE_KEY", "")
    container = base.replace("az://", "").replace("azure://", "").split("/")[0]
    if not account or not key:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT and AZURE_STORAGE_KEY are required for Azure reads")
    from azure.storage.blob import BlobServiceClient

    client = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )
    return client, container


def _download_azure_parquet_to_local(base: str, path: str, folder: str) -> str | None:
    """Download Azure parquet files for a dataset to a local temp folder.

    Uses a version-aware cache directory derived from the Azure path so
    files that were already downloaded for the same export version are
    re-used instantly without hitting Azure again.

    Returns a local parquet path/glob suitable for read_parquet.
    """
    try:
        client, container = _azure_blob_client(base)
    except Exception as exc:
        raise RuntimeError(f"Azure blob client init failed: {exc}") from exc

    # Build a version-specific cache dir so different export versions
    # don't collide and we know when files are already present.
    version_slug = path.replace("/", "_").replace(":", "").replace("*", "")
    cache_root = (
        Path(tempfile.gettempdir())
        / "mma_front_parquet_cache"
        / folder.replace("/", "_")
        / version_slug[:120]  # keep it manageable
    )
    cache_root.mkdir(parents=True, exist_ok=True)

    # Strip az://<container>/ from the absolute dataset path if present
    path_prefix = f"az://{container}/"
    azure_path = path
    if azure_path.startswith(path_prefix):
        azure_path = azure_path[len(path_prefix):]

    # wildcard directory export (partitioned datasets)
    if "/**/*.parquet" in azure_path:
        blob_prefix = azure_path.split("/**/*.parquet", 1)[0].strip("/") + "/"

        # If we already downloaded files into this version dir, reuse them
        existing = list(cache_root.rglob("*.parquet"))
        if existing:
            return str(cache_root / "**" / "*.parquet")

        blobs = client.get_container_client(container).list_blobs(name_starts_with=blob_prefix)
        downloaded = 0
        for blob in blobs:
            if not blob.name.endswith(".parquet"):
                continue
            rel = blob.name[len(blob_prefix):]
            local_file = cache_root / rel
            if local_file.exists():
                downloaded += 1
                continue
            local_file.parent.mkdir(parents=True, exist_ok=True)
            data = client.get_blob_client(container=container, blob=blob.name).download_blob().readall()
            local_file.write_bytes(data)
            downloaded += 1
        if downloaded == 0:
            return None
        return str(cache_root / "**" / "*.parquet")

    # single parquet file
    blob_name = azure_path.strip("/")
    if not blob_name.endswith(".parquet"):
        return None
    local_file = cache_root / Path(blob_name).name
    if not local_file.exists():
        data = client.get_blob_client(container=container, blob=blob_name).download_blob().readall()
        local_file.write_bytes(data)
    return str(local_file)


@st.cache_resource
def _parquet_store() -> dict:
    """Shared in-memory store for cached DataFrames (no serialize/deserialize per hit)."""
    return {}


def _read_parquet(folder: str, base: str, prefix: str = "") -> pd.DataFrame:
    """Read a dashboard Parquet dataset into a DataFrame.

    Uses a @st.cache_resource backing store so the DataFrame is held by
    reference (no pickle round-trip on every access).  Manual TTL matches
    the FRONT_CACHE_TTL_SECONDS env var.
    """
    store = _parquet_store()
    key = (folder, base, prefix)
    ttl = _get_front_cache_ttl_seconds()
    entry = store.get(key)
    if entry is not None and (time.monotonic() - entry["ts"]) < ttl:
        return entry["df"]

    df = _read_parquet_uncached(folder, base, prefix)
    store[key] = {"df": df, "ts": time.monotonic()}
    return df


def _read_parquet_uncached(folder: str, base: str, prefix: str = "") -> pd.DataFrame:
    """Internal: actually load the Parquet — called only on cache miss."""
    folder_path = _apply_prefix(folder, prefix or None)
    path = _resolve_latest(base, folder_path)
    if path is None:
        return pd.DataFrame()

    download_error: Exception | None = None
    try:
        read_path = path
        if _is_azure(base):
            try:
                local_download = _download_azure_parquet_to_local(base, path, folder_path)
            except Exception as dl_exc:
                download_error = dl_exc
                local_download = None
            if local_download:
                read_path = local_download
            else:
                # SDK returned None = 0 blobs found. Don't hand az:// to DuckDB
                # (it has SSL issues). Surface the problem immediately.
                hint = (
                    f"Azure SDK downloaded 0 files for folder '{folder_path}'. "
                    "The prefix is likely wrong — make sure the 'Parquet prefix' "
                    "field in the sidebar matches where the ETL wrote files "
                    "(e.g. mma/diamond)."
                )
                if download_error is not None:
                    hint = f"Azure SDK error for '{folder}': {download_error}"
                raise RuntimeError(hint)

        conn = _reader(base)
        return conn.execute(
            f"SELECT * FROM read_parquet('{read_path}', hive_partitioning=true)"
        ).fetchdf()
    except Exception as exc:
        if _is_azure(base):
            if download_error is not None:
                logger.warning(
                    "Azure SDK pre-download failed for %s: %s. Trying local fallback...",
                    folder, download_error,
                )
            local_base = _get_local_fallback_base()
            local_path = _resolve_latest(local_base, folder_path)
            if local_path is not None:
                try:
                    local_conn = _reader(local_base)
                    df_local = local_conn.execute(
                        f"SELECT * FROM read_parquet('{local_path}', hive_partitioning=true)"
                    ).fetchdf()
                    logger.warning(
                        "Azure read failed for %s; using local fallback. Error: %s",
                        folder, exc,
                    )
                    return df_local
                except Exception as local_exc:
                    logger.warning(
                        "Could not read %s from Azure or local fallback. "
                        "Azure error: %s | Local error: %s",
                        folder, exc, local_exc,
                    )
                    return pd.DataFrame()

        logger.warning("Could not read %s: %s", folder, exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Dataset folder constants (must match ETL output)
# ---------------------------------------------------------------------------

FOLDER_UPCOMING  = "dashboard_upcoming_cards"
FOLDER_UPCOMING_ENSEMBLE = "dashboard_upcoming_cards_ensemble"
FOLDER_UPCOMING_CATBOOST = "dashboard_upcoming_cards_catboost"
FOLDER_UPCOMING_LOGREG = "dashboard_upcoming_cards_logreg"
FOLDER_EVENTS    = "dashboard_upcoming_events"
FOLDER_HIST_ALL      = "dashboard_hist_historical_all"
FOLDER_HIST_ALL_ENSEMBLE = "dashboard_hist_historical_all_ensemble"
FOLDER_HIST_ALL_CATBOOST = "dashboard_hist_historical_all_catboost"
FOLDER_HIST_ALL_LOGREG = "dashboard_hist_historical_all_logreg"
FOLDER_CAL       = "dashboard_calibration_buckets"
FOLDER_CAL_ENSEMBLE = "dashboard_calibration_buckets_ensemble"
FOLDER_CAL_CATBOOST = "dashboard_calibration_buckets_catboost"
FOLDER_CAL_LOGREG = "dashboard_calibration_buckets_logreg"
FOLDER_STATS     = "dashboard_model_stats"
FOLDER_STATS_ENSEMBLE = "dashboard_model_stats_ensemble"
FOLDER_STATS_CATBOOST = "dashboard_model_stats_catboost"
FOLDER_STATS_LOGREG = "dashboard_model_stats_logreg"
FOLDER_RANKINGS  = "dashboard_rankings"
FOLDER_FIGHTER_PROFILES = "dashboard_fighter_profiles"
FOLDER_FIGHTER_HISTORY = "dashboard_fighter_history"
FOLDER_BELT_HOLDERS = "dashboard_belt_holders"
FOLDER_TITLE_FIGHT_HISTORY = "dashboard_title_fight_history"
FOLDER_MANUAL_TITLE_VACATES = "dashboard_manual_title_vacates"
FOLDER_FEATURE_IMPORTANCE_CATBOOST = "dashboard_feature_importance_catboost"
FOLDER_HPARAM_IMPORTANCE_CATBOOST = "dashboard_hparam_importance_catboost"
FOLDER_TUNE_TRIALS_CATBOOST = "dashboard_tune_trials_catboost"


@st.cache_data(ttl=900, show_spinner=False)
def _load_prepared_upcoming_cards(folder: str, base: str, prefix: str = "") -> pd.DataFrame:
    """Load, normalize, and sort upcoming cards once per cache window."""
    df = _read_parquet(folder, base, prefix)
    if df.empty and folder != FOLDER_UPCOMING:
        df = _read_parquet(FOLDER_UPCOMING, base, prefix)
    if df.empty:
        return df

    out = df.copy()
    if "event_name" in out.columns:
        event_names = out["event_name"].astype(str).str.strip()
        out["event_name"] = event_names
        out = out[
            ~event_names.str.lower().isin({"", "nan", "nat", "none"})
        ].copy()
    if "event_date" in out.columns:
        out["event_date"] = pd.to_datetime(out["event_date"], errors="coerce")
    if "edge" in out.columns:
        out["_edge_abs"] = pd.to_numeric(out["edge"], errors="coerce").abs()
    else:
        out["_edge_abs"] = pd.NA

    return out.sort_values(
        by=["event_date", "_edge_abs"],
        ascending=[True, False],
        na_position="last",
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Fight Prophet MMA Predictions",
    page_icon=_get_default_page_icon(),
    layout="wide",
    initial_sidebar_state="auto",
)

# Block search-engine indexing of the Streamlit app shell. The marketing site
# at fightprophet.com is the canonical indexable surface; the app subdomain
# serves a query-param SPA whose pre-hydration HTML is empty and trips
# AdSense's "low value content" policy.
st.markdown(
    '<meta name="robots" content="noindex, nofollow">',
    unsafe_allow_html=True,
)

_inject_theme_aware_favicon()

_background_uri = _favicon_data_uri(_STATIC_DIR / "background.png")
_ear_overlay_uri = _favicon_data_uri(_STATIC_DIR / "fightprophet_ear_white.png")
_app_shell_background = (
    "linear-gradient(180deg, rgba(9, 9, 11, 0.52) 0%, rgba(9, 9, 11, 0.68) 32%, rgba(9, 9, 11, 0.80) 100%), "
    f"url('{_background_uri}') center top / cover no-repeat fixed, #09090b"
    if _background_uri
    else "#09090b"
)
_sidebar_nav_icon_css = "\n".join(
    (
        f".fp-sidebar-nav-icon--{_nav_icon_class(slug)}"
        "{background-image:url('"
        f"{_named_icon_data_uri(filename)}"
        "');}"
    )
    for slug, filename in _NAV_ICON_FILES.items()
    if _named_icon_data_uri(filename)
)

# Minimal custom CSS for dark theme polish
st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0,0');

    .material-symbols-rounded,
    .material-icons,
    [class*="material-symbols"] {
        font-family: "Material Symbols Rounded" !important;
        font-weight: 400;
        font-style: normal;
        line-height: 1;
        letter-spacing: normal;
        text-transform: none;
        display: inline-block;
        white-space: nowrap;
        word-wrap: normal;
        direction: ltr;
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
    }

    /* ── Fallback: hide raw Material Symbol ligature text globally.
       When the icon font fails to load, Streamlit renders ligature names
       like "arrow_right", "expand_more", etc. as visible text. Replace
       them with a simple CSS arrow so the UI never shows raw text. ── */
    /* ── Expander toggle icon: hide raw ligature text, show CSS arrow ── */
    [data-testid="stExpander"] .material-symbols-rounded,
    [data-testid="stExpander"] [class*="material-symbols"],
    [data-testid="stExpander"] [data-testid="stIconMaterial"],
    [data-testid="stExpanderToggleDetails"],
    summary [data-testid="stIconMaterial"] {
        font-size: 0 !important;
        line-height: 0 !important;
        color: transparent !important;
        overflow: hidden !important;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.1em;
        height: 1.1em;
        min-width: 1.1em;
        max-width: 1.1em;
        vertical-align: middle;
        flex-shrink: 0;
    }
    /* The wrapper span around the icon — keep it constrained */
    summary > span > span:first-child {
        display: inline-flex !important;
        align-items: center !important;
        width: 1.2em !important;
        min-width: 1.2em !important;
        max-width: 1.2em !important;
        overflow: hidden !important;
        flex-shrink: 0 !important;
    }
    [data-testid="stExpander"] .material-symbols-rounded::before,
    [data-testid="stExpander"] [class*="material-symbols"]::before,
    [data-testid="stExpander"] [data-testid="stIconMaterial"]::before,
    [data-testid="stExpanderToggleDetails"]::before,
    summary [data-testid="stIconMaterial"]::before {
        content: "▸";
        font-size: 0.92rem !important;
        line-height: 1 !important;
        color: #a1a1aa !important;
        display: inline-block;
        font-family: Inter, system-ui, sans-serif !important;
    }
    [data-testid="stExpander"][open] .material-symbols-rounded::before,
    [data-testid="stExpander"][open] [class*="material-symbols"]::before,
    [data-testid="stExpander"][open] [data-testid="stIconMaterial"]::before,
    [data-testid="stExpander"] details[open] .material-symbols-rounded::before,
    [data-testid="stExpander"] details[open] [class*="material-symbols"]::before,
    [data-testid="stExpander"] details[open] [data-testid="stIconMaterial"]::before,
    details[open] summary [data-testid="stIconMaterial"]::before {
        content: "▾";
    }

    .stApp,
    .stApp * {
        font-family: "Inter", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif !important;
    }

    /* Force dark app shell so white logos are always visible, with a subtle
       background texture behind the content. */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"] {
        background: __FP_APP_SHELL_BACKGROUND__ !important;
        background-attachment: fixed !important;
        background-position: center top !important;
        background-size: cover !important;
        color: #f4f4f5 !important;
    }

    [data-testid="stAppViewContainer"] {
        isolation: isolate !important;
    }

    [data-testid="stAppViewContainer"]::after {
        content: "" !important;
        position: fixed !important;
        left: 50% !important;
        top: 53% !important;
        width: min(32vw, 420px) !important;
        aspect-ratio: 1 / 1 !important;
        transform: translate(-50%, -50%) !important;
        background: url("__FP_EAR_OVERLAY_URI__") center center / contain no-repeat !important;
        opacity: 0.12 !important;
        filter: drop-shadow(0 0 22px rgba(220, 38, 38, 0.15)) !important;
        pointer-events: none !important;
        z-index: -1 !important;
    }

    [data-testid="stMainBlockContainer"] {
        background: transparent !important;
        color: #f4f4f5 !important;
    }

    :root {
        --fp-red-border-soft: rgba(220, 38, 38, 0.22);
        --fp-red-border-mid: rgba(220, 38, 38, 0.34);
        --fp-red-border-strong: rgba(248, 113, 113, 0.52);
        --fp-red-glow-soft: rgba(220, 38, 38, 0.12);
    }

    section[data-testid="stSidebar"] {
        background: #111113 !important;
        color: #f4f4f5 !important;
        border-right: 1px solid var(--fp-red-border-mid);
        box-shadow: inset -1px 0 0 rgba(127, 29, 29, 0.28);
    }

    section[data-testid="stSidebar"] * {
        color: #f4f4f5;
    }

    .fp-sidebar-heading {
        margin: 0.3rem 0 0.15rem;
        color: #f4f4f5;
        font-size: 1.08rem;
        font-weight: 700;
        letter-spacing: 0.01em;
    }
    .fp-sidebar-subheading {
        margin: 0 0 0.9rem;
        color: #a1a1aa;
        font-size: 0.84rem;
        line-height: 1.45;
    }
    .fp-sidebar-lang-switch {
        display: flex;
        width: 100%;
        max-width: 100%;
        justify-content: center;
        align-items: center;
        gap: 0.28rem;
        background: rgba(15, 15, 18, 0.64);
        border: 1px solid rgba(113, 113, 122, 0.42);
        border-radius: 9999px;
        padding: 0.2rem;
        box-sizing: border-box;
        margin: 0.1rem auto 1rem;
    }
    .fp-sidebar-lang-btn,
    .fp-sidebar-lang-btn:link,
    .fp-sidebar-lang-btn:visited {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2.35rem;
        height: 2.35rem;
        border-radius: 9999px;
        text-decoration: none !important;
        color: #f4f4f5 !important;
        background: transparent;
        font-size: 1.34rem;
        line-height: 1;
        transition: background 80ms ease, box-shadow 80ms ease, color 80ms ease;
    }
    .fp-sidebar-lang-btn:hover,
    .fp-sidebar-lang-btn:focus,
    .fp-sidebar-lang-btn:active {
        text-decoration: none !important;
        color: #fef2f2 !important;
        background: rgba(220, 38, 38, 0.12);
        box-shadow: none !important;
    }
    .fp-sidebar-lang-btn.is-active {
        background: linear-gradient(160deg, #ef4444, #b91c1c);
        box-shadow: 0 0 10px rgba(239, 68, 68, 0.35);
    }
    .fp-sidebar-nav {
        display: grid;
        gap: 0.22rem;
        margin: 0.35rem 0 0.95rem;
    }
    .fp-sidebar-nav-copy,
    .fp-icon-label {
        display: grid;
        grid-template-columns: 36px minmax(0, 1fr);
        align-items: center;
        column-gap: 0.9rem;
        width: 100%;
    }
    .fp-sidebar-nav-icon {
        display: inline-block;
        width: 36px;
        height: 36px;
        background-position: center;
        background-repeat: no-repeat;
        background-size: contain;
        vertical-align: middle;
        justify-self: center;
        opacity: 0.95;
        filter: brightness(1.45) saturate(1.45) contrast(1.18) drop-shadow(0 0 6px rgba(220, 38, 38, 0.26));
        transition: filter 120ms ease, transform 120ms ease, opacity 120ms ease;
    }
    __FP_SIDEBAR_NAV_ICON_CSS__
    .fp-inline-goat {
        display: inline-block;
        flex: 0 0 auto;
        vertical-align: middle;
        object-fit: contain;
    }
    .fp-inline-goat--signal {
        filter: drop-shadow(0 0 8px rgba(248, 113, 113, 0.28));
    }
    .fp-inline-goat.fp-inline-emoji--signal-low {
        filter:
            brightness(1.6)
            saturate(1.35)
            contrast(1.55)
            drop-shadow(0 0 0.6px rgba(255, 255, 255, 0.95))
            drop-shadow(0 0 1.2px rgba(255, 255, 255, 0.85))
            drop-shadow(0 0 14px rgba(248, 113, 113, 0.55));
    }
    .fp-inline-goat.fp-inline-emoji--kpi {
        filter: brightness(1.18) saturate(1.18) contrast(1.1) drop-shadow(0 0 10px rgba(248, 113, 113, 0.24));
    }
    .fp-inline-goat.fp-inline-emoji--kpi-heavy {
        filter: brightness(1.26) saturate(1.24) contrast(1.18) drop-shadow(0 0 12px rgba(248, 113, 113, 0.28));
    }
    .fp-inline-emoji {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        line-height: 1;
        vertical-align: middle;
        filter: drop-shadow(0 0 8px rgba(248, 113, 113, 0.18));
    }
    .fp-inline-emoji--signal {
        font-size: 0.92rem;
    }
    .fp-inline-emoji--signal-low {
        font-size: 1.35rem;
        font-weight: 900;
        filter:
            drop-shadow(0 0 0.6px rgba(255, 255, 255, 0.9))
            drop-shadow(0 0 14px rgba(248, 113, 113, 0.5));
    }
    .fp-inline-emoji--guide {
        font-size: 0.98rem;
    }
    .fp-inline-emoji--guide-value {
        font-size: 1.08rem;
        filter: drop-shadow(0 0 10px rgba(248, 113, 113, 0.28));
    }
    .fp-inline-emoji--kpi {
        font-size: 1.95rem;
        filter: drop-shadow(0 0 10px rgba(248, 113, 113, 0.24));
    }
    .fp-inline-emoji--kpi-heavy {
        font-size: 2.08rem;
        filter: drop-shadow(0 0 12px rgba(248, 113, 113, 0.28));
    }
    .fp-inline-emoji--versus {
        font-size: 1.7rem;
        filter: drop-shadow(0 0 14px rgba(248, 113, 113, 0.34));
    }
    .fp-inline-goat--champ {
        margin-left: 0.16rem;
        vertical-align: -2px;
    }
    .fp-sidebar-nav-link {
        display: flex;
        align-items: center;
        padding: 0.52rem 0.7rem;
        border-radius: 0.65rem;
        border: 1px solid transparent;
        text-decoration: none;
        font-size: 0.9rem;
        font-weight: 600;
        color: #d4d4d8 !important;
        transition: background 80ms ease, border-color 80ms ease, color 80ms ease;
    }
    .fp-sidebar-nav-link,
    .fp-sidebar-nav-link:link,
    .fp-sidebar-nav-link:visited,
    .fp-sidebar-nav-link:hover,
    .fp-sidebar-nav-link:focus,
    .fp-sidebar-nav-link:active {
        text-decoration: none !important;
        box-shadow: none !important;
    }
    .fp-sidebar-nav-link:hover {
        background: rgba(220, 38, 38, 0.12);
        border-color: rgba(220, 38, 38, 0.22);
        color: #fef2f2 !important;
    }
    .fp-sidebar-nav-link:hover .fp-sidebar-nav-icon {
        opacity: 1;
        transform: scale(1.04);
        filter: brightness(1.68) saturate(1.65) contrast(1.22) drop-shadow(0 0 10px rgba(248, 113, 113, 0.42));
    }
    .fp-sidebar-nav-link.is-active {
        background: rgba(220, 38, 38, 0.18);
        border-color: rgba(248, 113, 113, 0.38);
        color: #fef2f2 !important;
        font-weight: 700;
    }
    .fp-sidebar-nav-link.is-active .fp-sidebar-nav-icon {
        opacity: 1;
        transform: scale(1.05);
        filter: brightness(1.82) saturate(1.75) contrast(1.24) drop-shadow(0 0 12px rgba(248, 113, 113, 0.48));
    }
    .fp-section-title {
        display: inline-flex;
        align-items: center;
        gap: 0.62rem;
        margin: 0 0 1rem;
        color: #f4f4f5;
        font-size: 1.75rem;
        font-weight: 700;
        line-height: 1.2;
        letter-spacing: -0.01em;
    }

    /* Sidebar controls should stay dark (language flags, selectors, actions) */
    section[data-testid="stSidebar"] div[data-testid="stButton"] > button {
        background: rgba(39, 39, 42, 0.92) !important;
        border: 1px solid var(--fp-red-border-mid) !important;
        color: #f4f4f5 !important;
        font-size: 1.02rem !important;
        font-weight: 600 !important;
        min-height: 2.6rem !important;
        border-radius: 0.72rem !important;
        box-shadow: 0 0 0 1px rgba(127, 29, 29, 0.2), 0 0 10px var(--fp-red-glow-soft) !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stButton"] > button:hover {
        background: rgba(63, 45, 45, 0.9) !important;
        border-color: var(--fp-red-border-strong) !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stButton"] > button:focus,
    section[data-testid="stSidebar"] div[data-testid="stButton"] > button:focus-visible {
        box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.35) !important;
        outline: none !important;
    }
    section[data-testid="stSidebar"] [data-baseweb="select"] > div,
    section[data-testid="stSidebar"] [data-baseweb="input"] > div {
        background: rgba(24, 24, 27, 0.9) !important;
        border-color: var(--fp-red-border-mid) !important;
    }
    section[data-testid="stSidebar"] [data-baseweb="select"] input,
    section[data-testid="stSidebar"] [data-baseweb="input"] input {
        color: #f4f4f5 !important;
    }

    /* BaseWeb dropdown popovers (rendered outside sidebar tree) */
    div[data-baseweb="popover"] [role="listbox"] {
        background: #18181b !important;
        border: 1px solid var(--fp-red-border-mid) !important;
    }
    div[data-baseweb="popover"] [role="option"] {
        color: #e4e4e7 !important;
        background: #18181b !important;
    }
    div[data-baseweb="popover"] [role="option"][aria-selected="true"],
    div[data-baseweb="popover"] [role="option"]:hover {
        background: #27272a !important;
    }

    /* Sidebar navigation button spacing */
    section[data-testid="stSidebar"] div[data-testid="stButton"] {
        margin-bottom: 0.22rem;
    }

    .stMarkdown a {
        color: #93c5fd;
    }

    /* Remove default top padding */
    .block-container { padding-top: 2.0rem; }

    /* Hide Streamlit's white top decoration bar */
    [data-testid="stDecoration"],
    header[data-testid="stHeader"] {
        background: transparent !important;
        border-bottom: none !important;
    }
    [data-testid="stDecoration"] {
        display: none !important;
    }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: rgba(39, 39, 42, 0.5);
        border: 1px solid var(--fp-red-border-mid);
        border-radius: 0.75rem;
        padding: 1rem 1.25rem;
        box-shadow: 0 0 12px var(--fp-red-glow-soft);
    }
    [data-testid="stMetricValue"] { font-size: 1.6rem; }

    /* Signal badges in dataframes */
    .strong-signal { color: #4ade80; font-weight: 600; }
    .medium-signal { color: #facc15; font-weight: 600; }
    .weak-signal   { color: #a1a1aa; }

    /* Sidebar logos */
    section[data-testid="stSidebar"] [data-testid="stImage"] img {
        object-fit: contain;
        max-height: none;
        width: 100%;
        border: 0;
        background: transparent;
        padding: 0;
    }

    /* Sidebar toggle control: prevent raw material ligature text (keyboard_double_*)
       Covers legacy `collapsedControl` and newer Streamlit test IDs
       (`stSidebarCollapseButton`, `stSidebarCollapsedControl`, `baseButton-headerNoPadding`). */
    [data-testid="collapsedControl"],
    [data-testid="collapsedControl"] *,
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapseButton"] *,
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="stSidebarCollapsedControl"] *,
    [data-testid="stExpandSidebarButton"],
    [data-testid="stExpandSidebarButton"] *,
    [data-testid="stSidebarHeader"] button,
    [data-testid="stSidebarHeader"] button *,
    .fp-toggle-patched,
    .fp-toggle-patched * {
        color: transparent !important;
        font-size: 0 !important;
        line-height: 0 !important;
        text-shadow: none !important;
    }
    [data-testid="collapsedControl"] button,
    [data-testid="stSidebarCollapseButton"] button,
    [data-testid="stSidebarCollapsedControl"] button,
    [data-testid="stExpandSidebarButton"] button,
    [data-testid="stSidebarHeader"] button,
    .fp-toggle-patched {
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        min-width: 2rem !important;
        min-height: 2rem !important;
    }
    [data-testid="collapsedControl"] button::before,
    [data-testid="stSidebarCollapseButton"] button::before,
    [data-testid="stSidebarCollapsedControl"] button::before,
    [data-testid="stExpandSidebarButton"] button::before,
    [data-testid="stSidebarHeader"] button::before,
    .fp-toggle-patched::before {
        content: "☰";
        font-size: 1.08rem !important;
        line-height: 1 !important;
        color: #e4e4e7 !important;
        display: inline-block;
        font-family: Inter, system-ui, sans-serif !important;
    }
    [data-testid="collapsedControl"] button .fp-toggle-icon,
    [data-testid="stSidebarCollapseButton"] button .fp-toggle-icon,
    [data-testid="stSidebarCollapsedControl"] button .fp-toggle-icon,
    [data-testid="stExpandSidebarButton"] button .fp-toggle-icon,
    [data-testid="stSidebarHeader"] button .fp-toggle-icon {
        display: none !important;
    }
    [data-testid="collapsedControl"] button > span,
    [data-testid="stSidebarCollapseButton"] button > span,
    [data-testid="stSidebarCollapsedControl"] button > span,
    [data-testid="stExpandSidebarButton"] button > span,
    [data-testid="stSidebarHeader"] button > span,
    [data-testid="collapsedControl"] button [data-testid="stIconMaterial"],
    [data-testid="stSidebarCollapseButton"] button [data-testid="stIconMaterial"],
    [data-testid="stSidebarCollapsedControl"] button [data-testid="stIconMaterial"],
    [data-testid="stExpandSidebarButton"] button [data-testid="stIconMaterial"],
    [data-testid="stSidebarHeader"] button [data-testid="stIconMaterial"] {
        display: none !important;
    }
    div[role="tooltip"] {
        font-family: Inter, system-ui, sans-serif !important;
    }
    div[role="tooltip" i]:has(*),
    div[role="tooltip" i] {
        text-transform: none;
    }

    /* Fighter profile cards */
    .fighter-stat-card {
        background: linear-gradient(180deg, rgba(63, 63, 70, 0.60) 0%, rgba(39, 39, 42, 0.85) 100%);
        border: 1px solid var(--fp-red-border-mid);
        border-radius: 1rem;
        padding: 0.9rem 1rem;
        min-height: 110px;
        text-align: center;
        box-shadow: 0 0 12px var(--fp-red-glow-soft);
    }
    .fighter-stat-label {
        color: #ffffff;
        font-size: 0.92rem;
        line-height: 1.1;
        margin-bottom: 0.5rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .fighter-stat-value {
        color: #f4f4f5;
        font-size: 2.2rem;
        font-weight: 700;
        line-height: 1.05;
        letter-spacing: 0.01em;
    }

    .fighter-meta-card {
        background: linear-gradient(145deg, rgba(24, 24, 27, 0.78), rgba(39, 39, 42, 0.55));
        border: 1px solid var(--fp-red-border-soft);
        border-radius: 0.75rem;
        padding: 0.4rem 0.55rem;
        min-height: 56px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        text-align: center;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03), 0 4px 12px rgba(0,0,0,0.14);
        border-top: 3px solid var(--meta-accent, #ef4444);
    }
    .fighter-meta-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 16px;
        line-height: 1;
        margin-bottom: 0.15rem;
        filter: saturate(1.15);
    }
    .fighter-meta-label {
        color: #d4d4d8;
        font-size: 0.62rem;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        margin-bottom: 0.1rem;
        font-weight: 600;
    }
    .fighter-meta-value {
        color: #ffffff;
        font-size: 0.88rem;
        font-weight: 700;
        line-height: 1.1;
        text-wrap: balance;
    }

    .kpi-card {
        background: linear-gradient(145deg, rgba(24, 24, 27, 0.82), rgba(39, 39, 42, 0.62));
        border: 1px solid var(--fp-red-border-soft);
        border-top: 2px solid var(--fp-red-border-mid);
        border-radius: 1rem;
        min-height: 96px;
        padding: 0.7rem 0.72rem;
        text-align: center;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 0.12rem;
        margin-bottom: 0.5rem;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03), 0 4px 14px rgba(0,0,0,0.16), 0 0 10px var(--fp-red-glow-soft);
    }
    .kpi-card-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 46px;
        margin-bottom: 0.24rem;
        line-height: 1;
        color: #cbd5e1;
    }
    .kpi-card-label {
        color: #d4d4d8;
        font-size: 0.60rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.16rem;
        font-weight: 600;
        line-height: 1.22;
        word-break: break-word;
    }
    .kpi-card-value {
        color: #ffffff;
        font-size: 1.10rem;
        font-weight: 700;
        line-height: 1.03;
        letter-spacing: 0.01em;
    }
    .kpi-card-value--stack {
        display: flex;
        flex-direction: column;
        gap: 0.18rem;
        line-height: 1.05;
    }
    .kpi-card-value-line {
        display: flex;
        align-items: baseline;
        justify-content: center;
        gap: 0.36rem;
        flex-wrap: wrap;
    }
    .kpi-card-value-line strong {
        color: #ffffff;
        font-size: 1rem;
        font-weight: 800;
        letter-spacing: 0.01em;
    }
    .kpi-card-value-line span {
        color: #a1a1aa;
        font-size: 0.62rem;
        font-weight: 700;
        letter-spacing: 0.07em;
        text-transform: uppercase;
    }

    .result-badge {
        display: inline-block;
        padding: 0.15rem 0.48rem;
        border-radius: 999px;
        font-size: 0.76rem;
        font-weight: 700;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        border: 1px solid rgba(113, 113, 122, 0.45);
    }
    .result-win {
        color: #16a34a;
        background: rgba(34, 197, 94, 0.14);
    }
    .result-loss {
        color: #ef4444;
        background: rgba(239, 68, 68, 0.12);
    }
    .result-nc {
        color: #eab308;
        background: rgba(234, 179, 8, 0.14);
    }
    .kpi-card--compact {
        min-height: 80px;
        padding: 0.48rem 0.55rem;
    }
    .kpi-card-label--large {
        font-size: 0.72rem;
        letter-spacing: 0.06em;
    }

    .fighter-overview-card {
        background: linear-gradient(145deg, rgba(24, 24, 27, 0.82), rgba(39, 39, 42, 0.66));
        border: 1px solid var(--fp-red-border-mid);
        border-radius: 1rem;
        padding: 0.9rem 1rem;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03), 0 6px 16px rgba(0,0,0,0.18), 0 0 12px var(--fp-red-glow-soft);
    }
    .fighter-overview-card--summary {
        padding: 0.95rem 1rem 1rem;
        background: linear-gradient(145deg, rgba(28, 28, 32, 0.9), rgba(39, 39, 42, 0.72));
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03), 0 8px 18px rgba(0,0,0,0.2), 0 0 14px var(--fp-red-glow-soft);
    }
    .fighter-overview-card--strip {
        padding: 0.95rem 1rem 1rem;
        background: linear-gradient(145deg, rgba(28, 28, 32, 0.92), rgba(39, 39, 42, 0.76));
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03), 0 8px 18px rgba(0,0,0,0.2), 0 0 14px var(--fp-red-glow-soft);
    }
    .fighter-overview-title {
        color: #f4f4f5;
        font-size: 0.84rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        margin-bottom: 0.6rem;
    }
    .fighter-overview-title--summary {
        font-size: 0.78rem;
        color: #d4d4d8;
        margin-bottom: 0.72rem;
    }
    .fighter-overview-title--strip {
        font-size: 0.78rem;
        color: #d4d4d8;
        margin-bottom: 0.62rem;
    }
    .fighter-overview-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 0.55rem;
    }
    .fighter-overview-grid--summary {
        gap: 0.65rem;
    }
    .fighter-overview-grid--strip {
        grid-template-columns: repeat(6, minmax(0, 1fr));
        gap: 0;
        background: rgba(255,255,255,0.025);
        border: 1px solid rgba(248, 113, 113, 0.14);
        border-radius: 0.88rem;
        overflow: hidden;
    }
    .fighter-overview-item {
        background: rgba(39, 39, 42, 0.68);
        border: 1px solid var(--fp-red-border-soft);
        border-radius: 0.7rem;
        padding: 0.48rem 0.58rem;
        min-height: 58px;
    }
    .fighter-overview-item--summary {
        min-height: 104px;
        padding: 0.72rem 0.78rem;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
    }
    .fighter-overview-item--strip {
        min-height: 0;
        padding: 0.78rem 0.72rem 0.72rem;
        background: transparent;
        border: 0;
        border-right: 1px solid rgba(248, 113, 113, 0.12);
        border-radius: 0;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 0.16rem;
        text-align: center;
    }
    .fighter-overview-item--strip:last-child {
        border-right: 0;
    }
    .fighter-overview-label {
        color: #a1a1aa;
        font-size: 0.66rem;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        margin-bottom: 0.18rem;
        font-weight: 600;
    }
    .fighter-overview-value {
        color: #f4f4f5;
        font-size: 0.92rem;
        font-weight: 700;
        line-height: 1.15;
    }
    .fighter-overview-value--summary {
        font-size: 1.6rem;
        font-weight: 800;
        line-height: 1.05;
        letter-spacing: -0.02em;
    }
    .fighter-overview-value--strip {
        font-size: 1.22rem;
        font-weight: 800;
        line-height: 1.05;
        letter-spacing: -0.01em;
    }

    /* Larger reading copy for static content pages */
    .page-copy p,
    .page-copy li {
        font-size: 1.08rem;
        line-height: 1.65;
    }
    .page-copy h3 {
        font-size: 1.65rem;
        margin-top: 0.4rem;
    }

    /* ── Dark-themed dataframes (st.dataframe widget) ── */
    [data-testid="stDataFrame"],
    [data-testid="stTable"],
    .stDataFrame {
        background-color: #18181b !important;
        border-radius: 0.75rem;
        border: 1px solid var(--fp-red-border-mid) !important;
        box-shadow: 0 0 12px var(--fp-red-glow-soft);
    }
    [data-testid="stDataFrame"] table,
    [data-testid="stTable"] table {
        color: #e4e4e7 !important;
    }
    [data-testid="stDataFrame"] th {
        background-color: #27272a !important;
        color: #a1a1aa !important;
        border-bottom: 1px solid rgba(113, 113, 122, 0.35) !important;
    }
    [data-testid="stDataFrame"] td {
        background-color: #18181b !important;
        color: #e4e4e7 !important;
        border-bottom: 1px solid rgba(63, 63, 70, 0.4) !important;
    }
    /* Glide data-grid internals used by st.dataframe */
    [data-testid="stDataFrame"] [data-testid="glideDataEditor"],
    [data-testid="stDataFrame"] canvas {
        background-color: #18181b !important;
    }
    /* Override glide-data-grid CSS custom properties for dark cells */
    [data-testid="stDataFrame"] {
        --gdg-bg-cell: #18181b !important;
        --gdg-bg-cell-medium: #1f1f23 !important;
        --gdg-bg-header: #27272a !important;
        --gdg-bg-header-has-focus: #27272a !important;
        --gdg-bg-header-hovered: #3f3f46 !important;
        --gdg-text-dark: #e4e4e7 !important;
        --gdg-text-medium: #a1a1aa !important;
        --gdg-text-light: #71717a !important;
        --gdg-text-header: #a1a1aa !important;
        --gdg-border-color: rgba(63, 63, 70, 0.4) !important;
        --gdg-accent-color: #3b82f6 !important;
        --gdg-accent-light: rgba(59, 130, 246, 0.15) !important;
        --gdg-bg-bubble: #27272a !important;
        --gdg-text-bubble: #e4e4e7 !important;
    }
    /* Also force iframe-based rendering dark */
    [data-testid="stDataFrame"] iframe {
        background-color: #18181b !important;
    }
    /* ── Dark-themed raw HTML tables (.to_html) ── */
    .stMarkdown table {
        width: 100%;
        border-collapse: collapse;
        background-color: rgba(24, 24, 27, 0.88);
        color: #e4e4e7;
        border-radius: 0.5rem;
        overflow: hidden;
        border: 1px solid var(--fp-red-border-mid);
    }
    .stMarkdown table th {
        background-color: rgba(39, 39, 42, 0.92);
        color: #a1a1aa;
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 0.55rem 0.7rem;
        border-bottom: 1px solid rgba(113, 113, 122, 0.35);
    }
    .stMarkdown table td {
        padding: 0.5rem 0.7rem;
        border-bottom: 1px solid rgba(63, 63, 70, 0.35);
        font-size: 0.88rem;
    }
    .stMarkdown table tr:hover td {
        background-color: rgba(74, 34, 34, 0.38);
    }

    /* Tabs (home workflow + other Streamlit tab lists) */
    div[data-baseweb="tab-list"] {
        border-bottom: 1px solid var(--fp-red-border-mid) !important;
    }
    div[data-baseweb="tab-list"] button[role="tab"] {
        border-bottom: 2px solid transparent !important;
    }
    div[data-baseweb="tab-list"] button[role="tab"][aria-selected="true"] {
        border-bottom-color: rgba(248, 113, 113, 0.9) !important;
        box-shadow: 0 2px 0 rgba(239, 68, 68, 0.35) !important;
    }
    .stMarkdown table a {
        color: #38bdf8;
        text-decoration: none;
    }
    .stMarkdown table a:hover {
        text-decoration: underline;
    }

    /* ══════════════════════════════════════════════════════════════
       RESPONSIVE / MOBILE STYLES
       ══════════════════════════════════════════════════════════════ */

    /* ── Sidebar: overlay on mobile instead of pushing content ── */
    @media (max-width: 768px) {
        section[data-testid="stSidebar"] {
            z-index: 999 !important;
            position: fixed !important;
            top: 0 !important;
            left: 0 !important;
            height: 100vh !important;
            width: min(78vw, 290px) !important;
            min-width: unset !important;
            max-width: min(78vw, 290px) !important;
            overflow-y: auto !important;
            -webkit-overflow-scrolling: touch !important;
            overscroll-behavior: contain !important;
            box-shadow: 4px 0 24px rgba(0, 0, 0, 0.5) !important;
            transition: transform 0.25s ease !important;
        }
        /* When collapsed, slide off-screen */
        section[data-testid="stSidebar"][aria-expanded="false"] {
            transform: translateX(-100%) !important;
        }
        /* Prevent main content from shifting when sidebar opens */
        [data-testid="stAppViewContainer"] {
            margin-left: 0 !important;
        }
        /* Reduce block container padding on mobile */
        .block-container {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-top: 1rem !important;
        }
        /* Make sidebar nav buttons taller for touch */
        section[data-testid="stSidebar"] div[data-testid="stButton"] > button {
            min-height: 3rem !important;
            font-size: 1.05rem !important;
            padding: 0.5rem 0.75rem !important;
        }
    }

    /* ── Small phones: even narrower sidebar ── */
    @media (max-width: 480px) {
        section[data-testid="stSidebar"] {
            width: min(70vw, 245px) !important;
            max-width: min(70vw, 245px) !important;
        }
    }

    /* ── Tablet breakpoint ── */
    @media (max-width: 1024px) and (min-width: 769px) {
        .block-container {
            padding-left: 1.25rem !important;
            padding-right: 1.25rem !important;
        }
        section[data-testid="stSidebar"] {
            width: 260px !important;
            min-width: 260px !important;
        }
    }

    /* ── Metric cards: stack on narrow screens ── */
    @media (max-width: 640px) {
        [data-testid="stMetric"] {
            padding: 0.65rem 0.75rem !important;
        }
        [data-testid="stMetricValue"] {
            font-size: 1.25rem !important;
        }
    }

    /* ── Fighter overview grid: 2 cols on mobile, 4 on desktop ── */
    @media (max-width: 640px) {
        .fighter-overview-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            gap: 0.4rem !important;
        }
        .fighter-overview-grid--strip {
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            gap: 0 !important;
        }
        .fighter-overview-item {
            min-height: 48px !important;
            padding: 0.38rem 0.45rem !important;
        }
        .fighter-overview-item--summary {
            min-height: 90px !important;
            padding: 0.56rem 0.62rem !important;
        }
        .fighter-overview-item--strip {
            min-height: 0 !important;
            padding: 0.62rem 0.5rem !important;
            border-right: 1px solid rgba(248, 113, 113, 0.12) !important;
            border-bottom: 1px solid rgba(248, 113, 113, 0.12) !important;
        }
        .fighter-overview-item--strip:nth-child(2n) {
            border-right: 0 !important;
        }
        .fighter-overview-item--strip:nth-last-child(-n+2) {
            border-bottom: 0 !important;
        }
        .fighter-overview-value--summary {
            font-size: 1.2rem !important;
        }
        .fighter-overview-value--strip {
            font-size: 1.02rem !important;
        }
        .fighter-stat-card {
            min-height: 80px !important;
            padding: 0.6rem 0.7rem !important;
        }
        .fighter-stat-value {
            font-size: 1.6rem !important;
        }
    }
    @media (min-width: 641px) and (max-width: 900px) {
        .fighter-overview-grid {
            grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
        }
        .fighter-overview-grid--strip {
            grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
        }
        .fighter-overview-item--strip {
            border-bottom: 1px solid rgba(248, 113, 113, 0.12) !important;
        }
        .fighter-overview-item--strip:nth-child(3n) {
            border-right: 0 !important;
        }
        .fighter-overview-item--strip:nth-last-child(-n+3) {
            border-bottom: 0 !important;
        }
    }

    /* ── KPI cards: slightly smaller on mobile ── */
    @media (max-width: 640px) {
        .kpi-card {
            min-height: 82px !important;
            padding: 0.55rem 0.58rem !important;
        }
        .kpi-card-value {
            font-size: 1.0rem !important;
        }
        .kpi-card-label {
            font-size: 0.6rem !important;
            line-height: 1.2 !important;
        }
    }

    /* ── Fighter meta cards: tighter on mobile ── */
    @media (max-width: 640px) {
        .fighter-meta-card {
            min-height: 46px !important;
            padding: 0.3rem 0.4rem !important;
        }
        .fighter-meta-value {
            font-size: 0.78rem !important;
        }
    }

    /* ── HTML tables: horizontal scroll on overflow ── */
    /* Only scroll when there IS a table – avoids clipping fight-card name text */
    .stMarkdown:has(table) {
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch;
    }
    .stMarkdown table {
        min-width: 500px;
    }
    @media (max-width: 640px) {
        .stMarkdown table th,
        .stMarkdown table td {
            padding: 0.4rem 0.5rem !important;
            font-size: 0.78rem !important;
            white-space: nowrap;
        }
    }

    /* ── Dataframe widget: scroll on small screens ── */
    @media (max-width: 768px) {
        [data-testid="stDataFrame"] {
            max-width: 100% !important;
            overflow-x: auto !important;
        }
    }

    /* ── Plotly charts: constrain height on mobile ── */
    @media (max-width: 640px) {
        [data-testid="stPlotlyChart"] {
            max-height: 320px !important;
        }
    }

    /* ── Touch-friendly: larger clickable areas ── */
    @media (max-width: 768px) {
        /* Selectboxes */
        [data-baseweb="select"] > div {
            min-height: 2.75rem !important;
        }
        /* Radio buttons */
        [role="radiogroup"] label {
            min-height: 2.5rem !important;
            padding: 0.4rem 0.6rem !important;
        }
        /* Tabs */
        div[data-baseweb="tab-list"] button {
            min-height: 2.75rem !important;
            padding: 0.5rem 0.75rem !important;
        }
        /* Expander header */
        details summary {
            min-height: 2.75rem !important;
            padding: 0.5rem !important;
        }
        /* Slider */
        [data-baseweb="slider"] [role="slider"] {
            width: 28px !important;
            height: 28px !important;
        }
    }

    /* ── Page copy: readable on mobile ── */
    @media (max-width: 640px) {
        .page-copy p,
        .page-copy li {
            font-size: 0.95rem !important;
            line-height: 1.55 !important;
        }
        .home-tab-copy p,
        .home-tab-copy li {
            font-size: 0.95rem !important;
            line-height: 1.55 !important;
        }
    }

    /* ── Streamlit columns: force full-width stacking on mobile ── */
    @media (max-width: 640px) {
        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
        }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 100% !important;
        }
    }
    /* Tablet: allow 2-col stacking */
    @media (min-width: 641px) and (max-width: 900px) {
        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
        }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            min-width: 45% !important;
            flex: 1 1 45% !important;
        }
    }
</style>
"""
    .replace("__FP_APP_SHELL_BACKGROUND__", _app_shell_background)
    .replace("__FP_EAR_OVERLAY_URI__", _ear_overlay_uri or "")
    .replace("__FP_SIDEBAR_NAV_ICON_CSS__", _sidebar_nav_icon_css),
    unsafe_allow_html=True,
)

_inject_marketing_handoff_bridge()


def _render_site_shell_link(label: str, href: str, icon_slug: str, *, is_active: bool = False) -> str:
    classes = "fp-sidebar-nav-link fp-site-shell-link"
    if is_active:
        classes += " is-active"
    icon_class = _nav_icon_class(icon_slug)
    return (
        f'<a class="{classes}" href="{escape(href, quote=True)}">'
        f'<span class="fp-sidebar-nav-copy"><span class="fp-sidebar-nav-icon fp-sidebar-nav-icon--{escape(icon_class)}" aria-hidden="true"></span><span>{escape(label)}</span></span>'
        "</a>"
    )


def _render_sidebar_nav(
    active_slug: str,
    page_slug_to_label: dict[str, str],
    *,
    lang: str,
    leading_links: list[str] | None = None,
    trailing_links: list[str] | None = None,
) -> None:
    links: list[str] = []
    if leading_links:
        links.extend(leading_links)
    safe_lang = (lang or "en").strip().lower() or "en"
    for slug, label in page_slug_to_label.items():
        classes = "fp-sidebar-nav-link is-active" if slug == active_slug else "fp-sidebar-nav-link"
        href = f"?page={quote_plus(slug)}&lang={quote_plus(safe_lang)}"
        icon_class = _nav_icon_class(slug)
        links.append(
            f'<a class="{classes}" href="{escape(href, quote=True)}" target="_self">'
            f'<span class="fp-sidebar-nav-copy"><span class="fp-sidebar-nav-icon fp-sidebar-nav-icon--{escape(icon_class)}" aria-hidden="true"></span><span>{escape(label)}</span></span>'
            "</a>"
        )
    if trailing_links:
        links.extend(trailing_links)
    st.markdown(
        '<nav class="fp-sidebar-nav" aria-label="App sections">'
        + "".join(links)
        + "</nav>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    _ui_lang = _resolve_ui_lang()
    if _ui_lang not in _LANG_CODE_TO_LABEL:
        _ui_lang = "en"
    st.session_state["ui_lang"] = _ui_lang

    _requested_slug = _PAGE_SLUG_ALIASES.get(str(st.query_params.get("page", "")).strip().lower(), str(st.query_params.get("page", "")).strip().lower())
    _cookie_page_slug = _PAGE_SLUG_ALIASES.get(_cookie_get(_COOKIE_PAGE_SLUG, "").strip().lower(), _cookie_get(_COOKIE_PAGE_SLUG, "").strip().lower())
    _initial_slug = _requested_slug or _cookie_page_slug
    _sidebar_page_slug = _initial_slug or "predictions"

    _render_sidebar_primary_logo()
    st.caption(f"{t('sidebar.language')}: {_LANG_CODE_TO_LABEL.get(_ui_lang, 'English')}")
    _selected_lang = _render_sidebar_lang_switch(_ui_lang, _sidebar_page_slug)
    st.session_state["ui_lang"] = _selected_lang
    if _selected_lang != _ui_lang or str(st.query_params.get("lang", "")).strip().lower() != _selected_lang:
        st.query_params["lang"] = _selected_lang
    if _cookie_get(_COOKIE_LANG, "") != _selected_lang:
        _cookie_set(_COOKIE_LANG, _selected_lang)

    st.markdown(f'<div class="fp-sidebar-heading">{escape(t("sidebar.title"))}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="fp-sidebar-subheading">{escape(t("sidebar.caption"))}</div>', unsafe_allow_html=True)

    _page_slug_to_label = {
        "predictions": t('nav.predictions'),
        "fighter-card": t('nav.fighter_profile'),
        "belt-holders": t('nav.belt_holders'),
        "events-history": t('nav.events_history'),
        "rankings": t('nav.rankings'),
        "fight-lab": t('nav.historical'),
    }
    _legacy_streamlit_pages = {"terms"}
    _active_page_slug = (
        _initial_slug
        if _initial_slug in _page_slug_to_label or _initial_slug in _legacy_streamlit_pages
        else "predictions"
    )

    st.caption(t("sidebar.navigate"))
    _render_sidebar_nav(
        _active_page_slug,
        _page_slug_to_label,
        lang=_selected_lang,
        leading_links=[
            _render_site_shell_link(t("nav.home"), _MARKETING_HOME_URL, "rankings"),
        ],
        trailing_links=[
            _render_site_shell_link(t("nav.terms"), _MARKETING_TERMS_URL, "terms", is_active=_active_page_slug == "terms"),
        ],
    )

    st.query_params["page"] = _active_page_slug
    _cookie_set(_COOKIE_PAGE_SLUG, _active_page_slug)

    _inject_canonical_link(_active_page_slug)

    st.divider()

    _base, _prefix_input = _azure_sidebar_defaults()
    st.session_state["image_mode"] = "off"

    if _show_azure_test_controls() and _is_azure(_base):
        if st.button(t("sidebar.test_azure"), help=t("sidebar.test_azure_help")):
            with st.spinner(t("sidebar.connecting")):
                try:
                    from azure.storage.blob import BlobServiceClient
                    _acct = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
                    _key  = os.environ.get("AZURE_STORAGE_KEY", "")
                    _cont = _base.replace("az://", "").replace("azure://", "").split("/")[0]
                    if not _acct or not _key:
                        st.error(t("sidebar.azure_missing_creds"))
                    else:
                        _svc = BlobServiceClient(
                            account_url=f"https://{_acct}.blob.core.windows.net",
                            credential=_key,
                        )
                        _pfx = (_prefix_input.strip("/") + "/") if _prefix_input.strip("/") else ""
                        _blobs = list(_svc.get_container_client(_cont).list_blob_names(name_starts_with=_pfx, results_per_page=50))
                        _folders = sorted({b.split("/")[len(_pfx.split("/")) - 1] for b in _blobs if "/" in b[len(_pfx):]})
                        st.success(t("sidebar.azure_connected", container=_cont, prefix=_pfx))
                        for _f in _folders:
                            st.caption(f"Folder: {_f}")
                        if not _folders:
                            st.warning(t("sidebar.no_folders", prefix=_pfx))
                except Exception as _test_err:
                    st.error(t("sidebar.connection_failed", error=_test_err))

    _render_mma_news(location="sidebar")
    st.divider()
    _render_sidebar_footer_logo()

ACTIVE_PARQUET_BASE = _base
ACTIVE_PREFIX = _prefix_input.strip("/")


# ---------------------------------------------------------------------------
# Helper: signal badge
# ---------------------------------------------------------------------------


def _inline_emoji_html(emoji: str, *, extra_class: str = "") -> str:
    class_attr = "fp-inline-emoji"
    if extra_class:
        class_attr += f" {extra_class.strip()}"
    return f'<span class="{class_attr}" aria-hidden="true">{escape(emoji)}</span>'


_SIGNAL_ICONS = {
    "STRONG": _png_icon_html("b91c1c-signals-emoji.png", size=16, extra_class="fp-inline-emoji--signal", label="Strong signal")
    or _inline_emoji_html("🟢", extra_class="fp-inline-emoji--signal"),
    "MEDIUM": _png_icon_html("b91c1c-signals-mid-emoji.png", size=16, extra_class="fp-inline-emoji--signal", label="Mid signal")
    or _inline_emoji_html("🟡", extra_class="fp-inline-emoji--signal"),
    "MID": _png_icon_html("b91c1c-signals-mid-emoji.png", size=16, extra_class="fp-inline-emoji--signal", label="Mid signal")
    or _inline_emoji_html("🟡", extra_class="fp-inline-emoji--signal"),
    "WEAK": _png_icon_html("b91c1c-signals-low-emoji.png", size=26, extra_class="fp-inline-emoji--signal fp-inline-emoji--signal-low", label="Low signal")
    or _inline_emoji_html("⚪", extra_class="fp-inline-emoji--signal fp-inline-emoji--signal-low"),
    "LOW": _png_icon_html("b91c1c-signals-low-emoji.png", size=26, extra_class="fp-inline-emoji--signal fp-inline-emoji--signal-low", label="Low signal")
    or _inline_emoji_html("⚪", extra_class="fp-inline-emoji--signal fp-inline-emoji--signal-low"),
}


def _signal_icon(sig: str | None) -> str:
    if sig is None:
        return _SIGNAL_ICONS["WEAK"]
    return _SIGNAL_ICONS.get(sig.upper(), _SIGNAL_ICONS["WEAK"])


def _odds_display(odds: float | None) -> str:
    """Format American odds with +/- sign."""
    if odds is None or pd.isna(odds):
        return "—"
    return f"+{int(odds)}" if odds > 0 else str(int(odds))


def _fighter_profile_link(name: object) -> str:
    txt = "" if name is None else str(name).strip()
    if not txt or txt in {"Draw", "No Contest", "—"}:
        return escape(txt or "")
    href = _fighter_profile_href(txt)
    return f'<a href="{escape(href, quote=True)}">{escape(txt)}</a>' if href else escape(txt)


def _fighter_profile_href(name: object) -> str:
    txt = "" if name is None else str(name).strip()
    if not txt or txt in {"Draw", "No Contest", "—"}:
        return ""
    return f"?page=fighter-card&fighter={quote_plus(txt)}"


def _open_fighter_profile(fighter_name: str) -> None:
    if not fighter_name:
        return
    st.session_state["selected_fighter_profile"] = fighter_name
    st.query_params["page"] = "fighter-card"
    st.query_params["fighter"] = str(fighter_name)
    st.rerun()


def _render_fighter_stat_card(label: str, value: str) -> None:
    st.markdown(
        (
            '<div class="fighter-stat-card">'
            f'<div class="fighter-stat-label">{escape(label)}</div>'
            f'<div class="fighter-stat-value">{escape(value)}</div>'
            '</div>'
        ),
        unsafe_allow_html=True,
    )


def _render_fighter_meta_card(label: str, value: str, icon: str, accent: str) -> None:
    st.markdown(
        (
            f'<div class="fighter-meta-card" style="--meta-accent: {escape(accent)};">'
            f'<div class="fighter-meta-icon">{_icon_markup(icon, default_size=14)}</div>'
            f'<div class="fighter-meta-label">{escape(label)}</div>'
            f'<div class="fighter-meta-value">{escape(value)}</div>'
            '</div>'
        ),
        unsafe_allow_html=True,
    )


_FIGHTER_CARD_DIVISION_ABBREV = {
    "flyweight": "FLW",
    "bantamweight": "BW",
    "featherweight": "FW",
    "lightweight": "LW",
    "welterweight": "WW",
    "middleweight": "MW",
    "light heavyweight": "LHW",
    "heavyweight": "HW",
    "strawweight": "SW",
    "women's strawweight": "WSW",
    "women's flyweight": "WFLW",
    "women's bantamweight": "WBW",
    "women's featherweight": "WFW",
}

_FIGHTER_CARD_CSS = """
<style>
.fp-card{position:relative;display:flex;flex-direction:column;width:100%;max-width:252px;margin-inline:auto;aspect-ratio:5/7;min-height:292px;border-radius:1.1rem;padding:0.7rem 0.7rem 0.78rem;overflow:hidden;isolation:isolate;color:#fef2f2;font-family:Inter,system-ui,sans-serif;text-decoration:none;clip-path:polygon(11% 0,89% 0,100% 7%,100% 93%,89% 100%,11% 100%,0 93%,0 7%);transition:transform 120ms ease,box-shadow 120ms ease;}
.fp-card::before{content:'';position:absolute;inset:0;border-radius:inherit;z-index:-1;}
.fp-card::after{content:'';position:absolute;inset:0.32rem;border-radius:0.9rem;border:1px solid transparent;pointer-events:none;z-index:0;clip-path:polygon(9% 0,91% 0,100% 8%,100% 92%,91% 100%,9% 100%,0 92%,0 8%);}
.fp-card.is-champ::before{background:radial-gradient(120% 80% at 50% 0%,rgba(255,239,170,0.58),transparent 58%),radial-gradient(90% 64% at 50% 40%,rgba(255,248,220,0.14),transparent 70%),linear-gradient(160deg,#f8de76 0%,#c69218 35%,#765105 68%,#312000 100%);box-shadow:inset 0 0 0 1.5px rgba(255,235,150,0.55),0 0 18px rgba(245,158,11,0.32);}
.fp-card.is-default::before{background:radial-gradient(120% 80% at 50% 0%,rgba(184,194,219,0.36),transparent 60%),radial-gradient(75% 56% at 50% 38%,rgba(255,255,255,0.08),transparent 72%),linear-gradient(160deg,#343b47 0%,#1b1f26 54%,#090b0f 100%);box-shadow:inset 0 0 0 1px rgba(220,38,38,0.32),0 0 12px rgba(220,38,38,0.16);}
.fp-card.is-champ::after{border-color:rgba(255,244,180,0.56);box-shadow:inset 0 0 0 1px rgba(77,50,0,0.22),inset 0 0 18px rgba(255,238,160,0.2);}
.fp-card.is-default::after{border-color:rgba(250,204,21,0.22);box-shadow:inset 0 0 0 1px rgba(248,113,113,0.1),inset 0 0 16px rgba(148,163,184,0.09);}
.fp-card:hover{transform:translateY(-2px);}
.fp-card--compact{max-width:220px;min-height:250px;padding:0.62rem 0.62rem 0.68rem;}
.fp-card--compact .fp-card-portrait-silhouette{width:3.65rem;height:3.65rem;}
.fp-card--compact .fp-card-initials{font-size:1.92rem;}
.fp-card--compact .fp-card-name{font-size:0.92rem;margin:0 0 0.34rem;}
.fp-card--compact .fp-card-country{font-size:0.64rem;}
.fp-card--compact .fp-card-flag{font-size:1.34rem;padding:0.2rem 0.4rem;}
.fp-card--compact .fp-card-stat-val{min-width:2rem;font-size:0.76rem;}
.fp-card-crown{position:absolute;top:0.46rem;left:50%;transform:translateX(-50%);display:inline-flex;align-items:center;justify-content:center;line-height:1;z-index:3;text-shadow:0 0 6px rgba(0,0,0,0.55);}
.fp-card-chrome{position:absolute;inset:0;z-index:1;pointer-events:none;}
.fp-card-corner{position:absolute;width:1.15rem;height:1.15rem;border:1px solid rgba(255,255,255,0.16);background:radial-gradient(circle at 30% 30%,rgba(255,255,255,0.2),transparent 70%);transform:rotate(45deg);box-shadow:inset 0 0 10px rgba(255,255,255,0.08);}
.fp-card.is-champ .fp-card-corner{border-color:rgba(255,244,180,0.38);background:radial-gradient(circle at 30% 30%,rgba(255,244,180,0.24),transparent 72%);}
.fp-card-corner--tl{top:0.9rem;left:0.5rem;}
.fp-card-corner--tr{top:0.9rem;right:0.5rem;}
.fp-card-corner--bl{bottom:1rem;left:0.5rem;}
.fp-card-corner--br{bottom:1rem;right:0.5rem;}
.fp-card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:0.4rem;position:relative;z-index:2;}
.fp-card-rating{display:flex;flex-direction:column;align-items:center;line-height:1;gap:0.05rem;background:rgba(7,10,16,0.28);padding:0.2rem 0.34rem 0.24rem;border-radius:0.55rem;box-shadow:inset 0 0 0 1px rgba(255,255,255,0.07);}
.fp-card-rating-num{font-size:1.05rem;font-weight:800;letter-spacing:0.02em;text-shadow:0 1px 2px rgba(0,0,0,0.55);}
.fp-card.is-champ .fp-card-rating-num{color:#1f1300;text-shadow:0 1px 0 rgba(255,235,150,0.6);}
.fp-card.is-default .fp-card-rating-num{color:#fef2f2;}
.fp-card-rating-pos{font-size:0.62rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;}
.fp-card.is-champ .fp-card-rating-pos{color:rgba(31,19,0,0.8);}
.fp-card.is-default .fp-card-rating-pos{color:rgba(255,255,255,0.78);}
.fp-card-flag{font-size:1.56rem;line-height:1;background:rgba(0,0,0,0.34);border-radius:999px;padding:0.24rem 0.44rem;box-shadow:inset 0 0 0 1px rgba(255,255,255,0.08);}
.fp-card-portrait{position:relative;display:flex;align-items:center;justify-content:center;flex:1;margin:0.42rem 0 0.42rem;border-radius:1rem 1rem 0.85rem 0.85rem;background:radial-gradient(circle at 50% 30%,rgba(255,255,255,0.16),transparent 52%),linear-gradient(180deg,rgba(255,255,255,0.06),rgba(0,0,0,0.04));box-shadow:inset 0 0 0 1px rgba(255,255,255,0.06),inset 0 -18px 28px rgba(0,0,0,0.22);overflow:hidden;z-index:2;}
.fp-card-portrait-silhouette{display:inline-flex;align-items:center;justify-content:center;width:4.2rem;height:4.2rem;opacity:0.16;line-height:1;filter:blur(0.35px);}
.fp-card.is-champ .fp-card-portrait-silhouette{opacity:0.22;}
.fp-card-initials{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:2.15rem;font-weight:800;letter-spacing:0.07em;text-shadow:0 2px 10px rgba(0,0,0,0.75);}
.fp-card.is-champ .fp-card-initials{color:rgba(31,19,0,0.92);}
.fp-card.is-default .fp-card-initials{color:rgba(255,255,255,0.92);}
.fp-card-name{font-size:0.98rem;font-weight:800;letter-spacing:0.01em;text-align:center;line-height:1.12;margin:0 0 0.44rem;word-break:break-word;position:relative;z-index:2;padding:0.28rem 0.45rem;border-radius:999px;background:rgba(5,7,12,0.24);box-shadow:inset 0 0 0 1px rgba(255,255,255,0.05);}
.fp-card.is-champ .fp-card-name{color:#1f1300;}
.fp-card.is-default .fp-card-name{color:#fef2f2;}
.fp-card-meta{display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:0.28rem 0.36rem;margin:-0.08rem 0 0.34rem;min-height:1rem;position:relative;z-index:2;}
.fp-card-division{max-width:100%;font-size:0.58rem;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.fp-card-badge{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;padding:0.16rem 0.42rem;font-size:0.54rem;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;white-space:nowrap;}
.fp-card.is-champ .fp-card-division{color:rgba(31,19,0,0.82);}
.fp-card.is-default .fp-card-division{color:rgba(255,255,255,0.72);}
.fp-card.is-champ .fp-card-badge{background:rgba(31,19,0,0.14);color:rgba(31,19,0,0.92);box-shadow:inset 0 0 0 1px rgba(31,19,0,0.18);}
.fp-card.is-default .fp-card-badge{background:rgba(220,38,38,0.2);color:rgba(255,228,228,0.95);box-shadow:inset 0 0 0 1px rgba(248,113,113,0.22);}
.fp-card-badge--active,.fp-card.is-champ .fp-card-badge--active,.fp-card.is-default .fp-card-badge--active{background:rgba(34,197,94,0.16);color:#bbf7d0;box-shadow:inset 0 0 0 1px rgba(34,197,94,0.28);}
.fp-card-badge--inactive,.fp-card.is-champ .fp-card-badge--inactive,.fp-card.is-default .fp-card-badge--inactive{background:rgba(244,63,94,0.16);color:#fecdd3;box-shadow:inset 0 0 0 1px rgba(244,63,94,0.28);}
.fp-card-country{display:flex;align-items:center;justify-content:center;gap:0.34rem;margin:-0.05rem 0 0.38rem;font-size:0.72rem;font-weight:700;line-height:1.05;min-height:1rem;position:relative;z-index:2;}
.fp-card-country-flag{font-size:1.18rem;line-height:1;}
.fp-card-country-full,.fp-card-country-short{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.fp-card-country-short{display:none;letter-spacing:0.05em;text-transform:uppercase;}
.fp-card.is-champ .fp-card-country{color:rgba(31,19,0,0.84);}
.fp-card.is-default .fp-card-country{color:rgba(255,255,255,0.82);}
.fp-card-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0.22rem 0.32rem;background:linear-gradient(180deg,rgba(0,0,0,0.26),rgba(0,0,0,0.42)),repeating-linear-gradient(135deg,rgba(255,255,255,0.025) 0 7px,transparent 7px 14px);border-radius:0.72rem;padding:0.38rem 0.46rem;position:relative;z-index:2;box-shadow:inset 0 0 0 1px rgba(255,255,255,0.06);}
.fp-card.is-champ .fp-card-stats{background:rgba(31,19,0,0.28);}
.fp-card-stat{display:flex;align-items:center;justify-content:space-between;gap:0.25rem;line-height:1;padding:0.14rem 0.18rem;border-radius:0.42rem;background:rgba(255,255,255,0.05);}
.fp-card.is-champ .fp-card-stat{background:rgba(31,19,0,0.16);}
.fp-card-stat-label{font-size:0.56rem;font-weight:800;letter-spacing:0.08em;}
.fp-card-stat-label .fp-inline-goat{vertical-align:-2px;}
.fp-card-stat-icon{filter:brightness(1.14) saturate(1.1) contrast(1.08);}
.fp-card.is-champ .fp-card-stat-label{color:rgba(31,19,0,0.72);}
.fp-card.is-default .fp-card-stat-label{color:rgba(255,255,255,0.72);}
.fp-card-stat-val{display:inline-flex;align-items:center;justify-content:center;min-width:2.2rem;padding:0.16rem 0.28rem;border-radius:999px;font-size:0.82rem;font-weight:900;letter-spacing:0.01em;background:rgba(7,10,16,0.34);box-shadow:inset 0 0 0 1px rgba(255,255,255,0.08);}
.fp-card.is-champ .fp-card-stat-val{color:rgba(31,19,0,0.96);background:rgba(255,248,220,0.28);box-shadow:inset 0 0 0 1px rgba(77,50,0,0.12);}
.fp-card.is-default .fp-card-stat-val{color:rgba(255,255,255,0.96);}
@media (max-width:640px){.fp-card-country-full{display:none;}.fp-card-country-short{display:inline;}}
</style>
"""

_PREDICTION_MATCHUP_CSS = """
<style>
.fp-matchup-shell{position:relative;margin:0.85rem 0 1.15rem;padding:1rem 1rem 0.92rem;border-radius:1.45rem;overflow:hidden;background:linear-gradient(145deg,rgba(10,14,22,0.98) 0%,rgba(24,24,27,0.97) 50%,rgba(63,18,24,0.96) 100%);border:1px solid rgba(244,63,94,0.18);box-shadow:0 18px 40px rgba(0,0,0,0.28),inset 0 0 0 1px rgba(255,255,255,0.04);}
.fp-matchup-shell::before{content:'';position:absolute;inset:-20% auto auto 12%;width:260px;height:220px;background:radial-gradient(circle,rgba(239,68,68,0.18),transparent 62%);pointer-events:none;}
.fp-matchup-shell::after{content:'';position:absolute;inset:0;background:linear-gradient(130deg,rgba(255,255,255,0.03),transparent 18%,transparent 80%,rgba(248,113,113,0.06));pointer-events:none;}
.fp-matchup-head{position:relative;z-index:1;display:flex;align-items:center;justify-content:space-between;gap:0.75rem;flex-wrap:wrap;margin-bottom:0.95rem;}
.fp-matchup-head-copy{display:flex;flex-direction:column;gap:0.28rem;}
.fp-matchup-eyebrow{display:inline-flex;align-items:center;gap:0.45rem;width:fit-content;padding:0.28rem 0.65rem;border-radius:999px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);font-size:0.72rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#e4e4e7;}
.fp-matchup-subcopy{font-size:0.76rem;color:#a1a1aa;letter-spacing:0.04em;}
.fp-matchup-signal{display:inline-flex;align-items:center;gap:0.42rem;padding:0.34rem 0.72rem;border-radius:999px;font-size:0.74rem;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.08);color:#e4e4e7;}
.fp-matchup-signal--strong{color:#bbf7d0;border-color:rgba(34,197,94,0.24);background:rgba(34,197,94,0.12);}
.fp-matchup-signal--medium{color:#fde68a;border-color:rgba(234,179,8,0.24);background:rgba(234,179,8,0.12);}
.fp-matchup-signal--weak{color:#e4e4e7;border-color:rgba(161,161,170,0.22);background:rgba(113,113,122,0.1);}
.fp-matchup-signal--neutral{color:#f4f4f5;}
.fp-matchup-grid{position:relative;z-index:1;display:grid;grid-template-columns:minmax(0,1fr) 170px minmax(0,1fr);gap:0.95rem;align-items:center;}
.fp-fighter-pane{display:flex;flex-direction:column;align-items:center;gap:0.55rem;}
.fp-odds-chip{display:inline-flex;align-items:center;justify-content:center;gap:0.34rem;padding:0.28rem 0.7rem;border-radius:999px;background:rgba(0,0,0,0.34);border:1px solid rgba(255,255,255,0.08);font-size:0.74rem;font-weight:700;color:#f4f4f5;}
.fp-odds-chip-label{color:#a1a1aa;text-transform:uppercase;letter-spacing:0.08em;font-size:0.62rem;}
.fp-versus-core{display:flex;flex-direction:column;align-items:center;gap:0.72rem;padding:0.9rem 0.85rem;border-radius:1.1rem;background:linear-gradient(180deg,rgba(255,255,255,0.055),rgba(255,255,255,0.02));border:1px solid rgba(255,255,255,0.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,0.02);}
.fp-versus-mark{display:flex;align-items:center;justify-content:center;width:4.2rem;height:4.2rem;border-radius:999px;background:radial-gradient(circle at 30% 30%,rgba(248,113,113,0.42),rgba(127,29,29,0.18) 55%,rgba(0,0,0,0.08) 100%);border:1px solid rgba(248,113,113,0.25);box-shadow:0 0 26px rgba(239,68,68,0.22);color:#fee2e2;}
.fp-versus-title{font-size:0.68rem;font-weight:800;letter-spacing:0.12em;text-transform:uppercase;color:#d4d4d8;}
.fp-prob-stack{width:100%;display:flex;flex-direction:column;gap:0.42rem;}
.fp-prob-row{display:flex;flex-direction:column;gap:0.18rem;}
.fp-prob-meta{display:flex;justify-content:space-between;gap:0.4rem;font-size:0.74rem;color:#e4e4e7;}
.fp-prob-name{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.fp-prob-value{font-family:ui-monospace,SFMono-Regular,monospace;color:#fecaca;}
.fp-prob-track{height:0.4rem;border-radius:999px;background:rgba(255,255,255,0.08);overflow:hidden;}
.fp-prob-fill{height:100%;border-radius:inherit;background:linear-gradient(90deg,#f59e0b,#ef4444);}
.fp-prob-fill--alt{background:linear-gradient(90deg,#93c5fd,#38bdf8);}
.fp-pick-summary{position:relative;z-index:1;margin-top:1rem;padding:0.82rem 0.95rem;border-radius:1rem;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.07);}
.fp-pick-winner{font-size:1.04rem;font-weight:800;color:#fafafa;text-align:center;line-height:1.32;}
.fp-pick-winner strong{color:#fca5a5;text-transform:uppercase;letter-spacing:0.06em;font-size:0.7rem;margin-right:0.35rem;}
.fp-pick-winner a{color:#fff1f2;text-decoration:none;border-bottom:1px solid rgba(255,255,255,0.24);}
.fp-pick-confidence{font-size:0.92rem;font-weight:500;color:#d4d4d8;}
.fp-pick-value{margin-top:0.42rem;text-align:center;font-size:0.81rem;color:#a1a1aa;opacity:0.92;line-height:1.35;}
.fp-pick-value .fp-inline-goat{vertical-align:-3px;}
.fp-pick-value b{color:#e4e4e7;}
.fp-pick-value a{color:#f4f4f5;text-decoration:none;}
.fp-pick-note{margin-top:0.28rem;text-align:center;font-size:0.72rem;color:#71717a;}
@media (max-width:900px){.fp-matchup-grid{grid-template-columns:1fr;}.fp-versus-core{max-width:340px;margin:0 auto;}.fp-fighter-pane{max-width:260px;margin:0 auto;}}
</style>
"""

_BETTING_GUIDE_CSS = """
<style>
.fp-guide-shell{position:relative;margin:0.8rem 0 1rem;padding:0.95rem 1rem 1rem;border-radius:1.15rem;background:linear-gradient(145deg,rgba(24,24,27,0.9),rgba(39,39,42,0.72));border:1px solid rgba(244,63,94,0.16);box-shadow:inset 0 1px 0 rgba(255,255,255,0.03),0 8px 20px rgba(0,0,0,0.18);}
.fp-guide-header{display:flex;align-items:flex-end;justify-content:space-between;gap:0.75rem;flex-wrap:wrap;margin-bottom:0.82rem;}
.fp-guide-title{font-size:0.92rem;font-weight:800;letter-spacing:0.07em;text-transform:uppercase;color:#f4f4f5;}
.fp-guide-subtitle{font-size:0.78rem;color:#a1a1aa;max-width:58ch;}
.fp-guide-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0.7rem;}
.fp-guide-item{padding:0.78rem 0.82rem;border-radius:0.95rem;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);}
.fp-guide-item--strong{border-color:rgba(34,197,94,0.24);background:rgba(34,197,94,0.08);}
.fp-guide-item--medium{border-color:rgba(234,179,8,0.24);background:rgba(234,179,8,0.08);}
.fp-guide-item--weak{border-color:rgba(161,161,170,0.2);background:rgba(113,113,122,0.08);}
.fp-guide-item--recommended{border-color:rgba(139,92,246,0.24);background:rgba(139,92,246,0.08);}
.fp-guide-item--check{grid-column:1/-1;}
.fp-guide-label{display:flex;align-items:center;gap:0.45rem;margin-bottom:0.35rem;font-size:0.8rem;font-weight:800;color:#fafafa;letter-spacing:0.03em;}
.fp-guide-copy{font-size:0.78rem;line-height:1.45;color:#d4d4d8;}
.fp-guide-footer{margin-top:0.82rem;padding-top:0.75rem;border-top:1px solid rgba(255,255,255,0.06);font-size:0.74rem;line-height:1.45;color:#a1a1aa;}
.fp-guide-footer a{color:#fca5a5;text-decoration:none;}
@media (max-width:780px){.fp-guide-grid{grid-template-columns:1fr;}.fp-guide-item--check{grid-column:auto;}}
</style>
"""


def _fighter_card_division_abbrev(weight_class: str) -> str:
    wc = (weight_class or "").strip()
    if not wc:
        return ""
    key = wc.lower()
    if key in _FIGHTER_CARD_DIVISION_ABBREV:
        return _FIGHTER_CARD_DIVISION_ABBREV[key]
    return "".join(part[0] for part in wc.split() if part)[:4].upper()


def _fighter_card_flag(country: str) -> str:
    return _country_to_flag(country)


def _fighter_card_initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _fighter_card_fmt_pct(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        pct = float(value)
    except Exception:
        return "—"
    if pct <= 1.0:
        pct *= 100.0
    return f"{round(pct)}%"


def _fighter_card_fmt_int(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        return str(int(float(value)))
    except Exception:
        return "—"


def _fighter_card_country_short(country: str) -> str:
    return _shared_country_short_label(country)


def _build_fighter_card_html(
    *,
    name: str,
    country: str = "",
    weight_class: str = "",
    is_champion: bool = False,
    fighter_status: str = "",
    finish_rate: object = None,
    sub_rate: object = None,
    win_streak: object = None,
    loss_streak: object = None,
    wins: object = None,
    losses: object = None,
    compact: bool = False,
    href: str = "",
) -> str:
    """Build a FUTBIN-style fighter card matching FighterCard.astro."""
    initials = _fighter_card_initials(name)
    wc_abbr = _fighter_card_division_abbrev(weight_class)
    division_label = (weight_class or "").strip()
    flag = _fighter_card_flag(country)
    country_short = _fighter_card_country_short(country)
    status_raw = str(fighter_status or "").strip().lower()
    status_label = "Inactive" if status_raw == "inactive" else ("Active" if status_raw else "")
    wins_txt = _fighter_card_fmt_int(wins)
    losses_txt = _fighter_card_fmt_int(losses)
    record_label = (
        f"{wins_txt}-{losses_txt}"
        if wins_txt != "—" and losses_txt != "—"
        else wins_txt
    )
    card_classes = ["fp-card", "is-champ" if is_champion else "is-default"]
    if compact:
        card_classes.append("fp-card--compact")
    crown_html = (
        f"<span class='fp-card-crown' aria-label='Current champion'>{_goat_icon_html(size=18, label='Current champion')}</span>"
        if is_champion
        else ""
    )
    flag_html = (
        f"<span class='fp-card-flag' title='{escape(country)}'>{flag}</span>" if flag else ""
    )
    pos_html = f"<span class='fp-card-rating-pos'>{escape(wc_abbr)}</span>" if wc_abbr else ""
    meta_html = (
        "<div class='fp-card-meta'>"
        + (f"<span class='fp-card-division'>{escape(division_label)}</span>" if division_label else "")
        + ("<span class='fp-card-badge'>Champion</span>" if is_champion else "")
        + (
            f"<span class='fp-card-badge fp-card-badge--{'inactive' if status_label == 'Inactive' else 'active'}'>{escape(status_label)}</span>"
            if status_label else ""
        )
        + "</div>"
        if division_label or is_champion or status_label
        else ""
    )
    country_html = (
        "<div class='fp-card-country'>"
        + (f"<span class='fp-card-country-flag' aria-hidden='true'>{flag}</span>" if flag else "")
        + f"<span class='fp-card-country-full'>{escape(country)}</span>"
        + f"<span class='fp-card-country-short'>{escape(country_short)}</span>"
        + "</div>"
        if country
        else ""
    )
    win_streak_label = (
        _png_icon_html("b91c1c-correct-emoji.png", size=13, extra_class="fp-card-stat-icon", label="Win streak")
        or "W"
    )
    loss_streak_label = (
        _png_icon_html("b91c1c-incorrect-emoji.png", size=13, extra_class="fp-card-stat-icon", label="Loss streak")
        or "L"
    )
    tag = "a" if href else "div"
    href_attr = f" href='{escape(href, quote=True)}'" if href else ""
    rel_attr = " target='_self'" if href else ""

    return (
        f"<{tag} class='{' '.join(card_classes)}'{href_attr}{rel_attr}>"
        f"{crown_html}"
        "<div class='fp-card-chrome' aria-hidden='true'>"
        "<span class='fp-card-corner fp-card-corner--tl'></span>"
        "<span class='fp-card-corner fp-card-corner--tr'></span>"
        "<span class='fp-card-corner fp-card-corner--bl'></span>"
        "<span class='fp-card-corner fp-card-corner--br'></span>"
        "</div>"
        "<div class='fp-card-top'>"
        f"<div class='fp-card-rating'><span class='fp-card-rating-num'>{escape(record_label)}</span>{pos_html}</div>"
        f"{flag_html}"
        "</div>"
        "<div class='fp-card-portrait' aria-hidden='true'>"
        f"<span class='fp-card-portrait-silhouette'>{_goat_icon_html(size=28, label='Fighter card')}</span>"
        f"<span class='fp-card-initials'>{escape(initials)}</span>"
        "</div>"
        f"<div class='fp-card-name'>{escape(name)}</div>"
        f"{meta_html}"
        f"{country_html}"
        "<div class='fp-card-stats'>"
        f"<div class='fp-card-stat'><span class='fp-card-stat-label'>FIN</span><span class='fp-card-stat-val'>{_fighter_card_fmt_pct(finish_rate)}</span></div>"
        f"<div class='fp-card-stat'><span class='fp-card-stat-label'>SUB</span><span class='fp-card-stat-val'>{_fighter_card_fmt_pct(sub_rate)}</span></div>"
        f"<div class='fp-card-stat'><span class='fp-card-stat-label'>{win_streak_label}</span><span class='fp-card-stat-val'>{_fighter_card_fmt_int(win_streak)}</span></div>"
        f"<div class='fp-card-stat'><span class='fp-card-stat-label'>{loss_streak_label}</span><span class='fp-card-stat-val'>{_fighter_card_fmt_int(loss_streak)}</span></div>"
        "</div>"
        f"</{tag}>"
    )


def _render_fighter_card_html(
    *,
    name: str,
    country: str = "",
    weight_class: str = "",
    is_champion: bool = False,
    fighter_status: str = "",
    finish_rate: object = None,
    sub_rate: object = None,
    win_streak: object = None,
    loss_streak: object = None,
    wins: object = None,
    losses: object = None,
    compact: bool = False,
    href: str = "",
) -> None:
    """Render a FUTBIN-style fighter card matching FighterCard.astro."""
    body = _build_fighter_card_html(
        name=name,
        country=country,
        weight_class=weight_class,
        is_champion=is_champion,
        fighter_status=fighter_status,
        finish_rate=finish_rate,
        sub_rate=sub_rate,
        win_streak=win_streak,
        loss_streak=loss_streak,
        wins=wins,
        losses=losses,
        compact=compact,
        href=href,
    )
    st.markdown(_FIGHTER_CARD_CSS + body, unsafe_allow_html=True)


def _render_kpi_card(
    label: str,
    value: str,
    icon: str | None = None,
    accent: str = "#38bdf8",
    compact: bool = False,
    value_is_html: bool = False,
) -> None:
    card_class = "kpi-card kpi-card--compact" if compact else "kpi-card"
    label_class = "kpi-card-label kpi-card-label--large" if compact else "kpi-card-label"
    icon_markup = _icon_markup(icon, default_size=14)
    value_markup = value if value_is_html else escape(value)
    st.markdown(
        (
            f'<div class="{card_class}" style="--kpi-accent: {escape(accent)};">'
            f'<div class="kpi-card-icon">{icon_markup}</div>'
            f'<div class="{label_class}">{escape(label)}</div>'
            f'<div class="kpi-card-value">{value_markup}</div>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _render_betting_signals_guide() -> None:
    strong_icon = _signal_icon("STRONG")
    medium_icon = _signal_icon("MEDIUM")
    low_icon = _signal_icon("LOW")
    guide_html = (
        '<section class="fp-guide-shell">'
        '<div class="fp-guide-header">'
        '<div>'
        '<div class="fp-guide-title">Betting Signals Guide</div>'
        '<div class="fp-guide-subtitle">Read the card fast: confidence first, then price value.</div>'
        '</div>'
        '</div>'
        '<div class="fp-guide-grid">'
        '<div class="fp-guide-item fp-guide-item--strong">'
        f'<div class="fp-guide-label">{strong_icon} STRONG</div>'
        '<div class="fp-guide-copy">Higher-confidence signal based on model edge and agreement; shortlist first.</div>'
        '</div>'
        '<div class="fp-guide-item fp-guide-item--medium">'
        f'<div class="fp-guide-label">{medium_icon} MEDIUM</div>'
        '<div class="fp-guide-copy">Possible value, but needs extra checks like injuries, style matchup, and line movement.</div>'
        '</div>'
        '<div class="fp-guide-item fp-guide-item--weak">'
        f'<div class="fp-guide-label">{low_icon} LOW</div>'
        '<div class="fp-guide-copy">Low edge or noisy setup; usually a pass.</div>'
        '</div>'
        '<div class="fp-guide-item fp-guide-item--recommended">'
        f'<div class="fp-guide-label">{_png_icon_html("b91c1c-bets-emoji.png", size=20, extra_class="fp-inline-emoji--guide fp-inline-emoji--guide-value", label="Value flag") or _inline_emoji_html("✅", extra_class="fp-inline-emoji--guide fp-inline-emoji--guide-value")} VALUE FLAG</div>'
        '<div class="fp-guide-copy">Triggered internal value thresholds, but these flags are still high-variance and can lose often.</div>'
        '</div>'
        '<div class="fp-guide-item fp-guide-item--check">'
        '<div class="fp-guide-label">Quick check</div>'
        '<div class="fp-guide-copy">Model bars show win probability for each fighter. Top value angle only highlights the side that looks most mispriced versus the market.</div>'
        '</div>'
        '</div>'
        '<div class="fp-guide-footer">'
        'For education only. If you see data or logic that should improve, open a '
        '<a href="https://github.com/datatomas/fight_prophet/issues" target="_blank" rel="noopener noreferrer">GitHub issue</a> '
        'or comment on '
        '<a href="https://www.linkedin.com/company/fight-prophet" target="_blank" rel="noopener noreferrer">LinkedIn</a>.'
        '</div>'
        '</section>'
    )
    st.markdown(
        _BETTING_GUIDE_CSS + guide_html,
        unsafe_allow_html=True,
    )


def _compute_binary_metrics(df: pd.DataFrame) -> dict[str, float | int | None]:
    """Compute binary classification metrics from historical fight rows.

    Uses fighter-side probability (`model_prob`) against actual fighter-side outcome
    derived from `winner_name_display == fighter_name_display`.
    """
    required_cols = {"fighter_name_display", "winner_name_display", "model_prob"}
    if not required_cols.issubset(set(df.columns)):
        return {
            "n": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "auc": None,
            "brier": None,
            "log_loss": None,
        }

    work = df[["fighter_name_display", "winner_name_display", "model_prob"]].copy()
    work["fighter_name_display"] = work["fighter_name_display"].astype(str).str.strip()
    work["winner_name_display"] = work["winner_name_display"].astype(str).str.strip()
    work["model_prob"] = pd.to_numeric(work["model_prob"], errors="coerce")

    invalid_winner = {"", "nan", "nat", "none", "draw", "no contest"}
    winner_norm = work["winner_name_display"].str.lower()
    work = work[
        work["model_prob"].notna()
        & work["fighter_name_display"].ne("")
        & ~winner_norm.isin(invalid_winner)
    ].copy()

    if work.empty:
        return {
            "n": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "auc": None,
            "brier": None,
            "log_loss": None,
        }

    p = work["model_prob"].clip(0.0, 1.0)
    y_true = (work["winner_name_display"] == work["fighter_name_display"]).astype(int)
    y_pred = (p >= 0.5).astype(int)

    n = int(len(work))
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    accuracy = float((y_pred == y_true).mean()) if n else None
    precision = float(tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = float(tp / (tp + fn)) if (tp + fn) > 0 else None
    f1 = (
        float(2 * precision * recall / (precision + recall))
        if (precision is not None and recall is not None and (precision + recall) > 0)
        else None
    )

    # Rank-based AUC (equivalent to Mann-Whitney U), with tie handling.
    n_pos = int((y_true == 1).sum())
    n_neg = int((y_true == 0).sum())
    if n_pos > 0 and n_neg > 0:
        ranks = p.rank(method="average")
        rank_sum_pos = float(ranks[y_true == 1].sum())
        auc = (rank_sum_pos - (n_pos * (n_pos + 1) / 2.0)) / float(n_pos * n_neg)
        auc = float(max(0.0, min(1.0, auc)))
    else:
        auc = None

    brier = float(((p - y_true) ** 2).mean()) if n else None
    p_clip = p.clip(1e-6, 1 - 1e-6)
    log_loss = (
        float((-(y_true * p_clip.map(math.log) + (1 - y_true) * (1 - p_clip).map(math.log))).mean())
        if n
        else None
    )

    return {
        "n": n,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "auc": auc,
        "brier": brier,
        "log_loss": log_loss,
    }


def _render_fighter_overview_card(
    items: list[tuple[str, str]],
    title: str = "Fighter Vitals",
    html_value_labels: set[str] | None = None,
    emphasized: bool = False,
    layout: str = "grid",
) -> None:
    html_labels = html_value_labels or set()
    cells_parts: list[str] = []
    is_strip = layout == "strip"
    if is_strip:
        item_class = "fighter-overview-item fighter-overview-item--strip"
        value_class = "fighter-overview-value fighter-overview-value--strip"
        card_class = "fighter-overview-card fighter-overview-card--strip"
        title_class = "fighter-overview-title fighter-overview-title--strip"
        grid_class = "fighter-overview-grid fighter-overview-grid--strip"
    else:
        item_class = "fighter-overview-item fighter-overview-item--summary" if emphasized else "fighter-overview-item"
        value_class = "fighter-overview-value fighter-overview-value--summary" if emphasized else "fighter-overview-value"
        card_class = "fighter-overview-card fighter-overview-card--summary" if emphasized else "fighter-overview-card"
        title_class = "fighter-overview-title fighter-overview-title--summary" if emphasized else "fighter-overview-title"
        grid_class = "fighter-overview-grid fighter-overview-grid--summary" if emphasized else "fighter-overview-grid"
    for label, value in items:
        label_txt = escape(str(label))
        value_txt = "" if value is None else str(value)
        value_rendered = value_txt if str(label) in html_labels else escape(value_txt)
        cells_parts.append(
            f'<div class="{item_class}">'
            f'<div class="fighter-overview-label">{label_txt}</div>'
            f'<div class="{value_class}">{value_rendered}</div>'
            "</div>"
        )
    cells = "".join(cells_parts)
    st.markdown(
        (
            f'<div class="{card_class}">'
            f'<div class="{title_class}">{escape(title)}</div>'
            f'<div class="{grid_class}">{cells}</div>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _render_smart_dataframe(df: pd.DataFrame, *, key: str, height: int = 420) -> None:
    if AgGrid is not None and GridOptionsBuilder is not None:
        gb = GridOptionsBuilder.from_dataframe(df)
        gb.configure_default_column(filter=True, sortable=True, resizable=True)
        gb.configure_pagination(enabled=True, paginationAutoPageSize=True)
        gb.configure_grid_options(enableCellTextSelection=True)
        grid_options = gb.build()
        AgGrid(
            df,
            gridOptions=grid_options,
            height=height,
            theme="balham-dark",
            fit_columns_on_grid_load=False,
            key=key,
            allow_unsafe_jscode=False,
            reload_data=False,
        )
    else:
        st.dataframe(df, use_container_width=True, height=height)


def _normalize_search_text(value: object) -> str:
    txt = "" if value is None else str(value).strip().lower()
    txt = re.sub(r"[^a-z0-9\s\-]", "", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt


def _build_fighter_alias_map(df_profiles: pd.DataFrame, name_col: str) -> dict[str, set[str]]:
    alias_cols = [
        c for c in ["fighter_name_display", "fighter_name", "fighter_name_plain", "nickname"]
        if c in df_profiles.columns
    ]
    if not alias_cols or name_col not in df_profiles.columns:
        return {}

    alias_map: dict[str, set[str]] = {}
    for _, row in df_profiles.iterrows():
        base_name = row.get(name_col)
        if base_name is None or pd.isna(base_name):
            continue
        base_name_s = str(base_name).strip()
        if not base_name_s:
            continue
        aliases = alias_map.setdefault(base_name_s, set())
        aliases.add(base_name_s)
        for col in alias_cols:
            val = row.get(col)
            if val is None or pd.isna(val):
                continue
            val_s = str(val).strip()
            if not val_s:
                continue
            aliases.add(val_s)
            aliases.add(re.sub(r"\s*\([^)]*\)", "", val_s).strip())
    return alias_map


def _rank_fighter_options(
    options: list[str],
    query: str,
    alias_map: dict[str, set[str]] | None = None,
    limit: int = 80,
) -> list[str]:
    import difflib

    if not options:
        return []

    q = _normalize_search_text(query)
    if not q:
        return options[:limit]

    scored: list[tuple[int, float, str]] = []
    for name in options:
        aliases = alias_map.get(name, {name}) if alias_map else {name}
        norm_aliases = [_normalize_search_text(a) for a in aliases if _normalize_search_text(a)]
        if not norm_aliases:
            continue

        best_bucket = 99
        best_sim = 0.0
        for n in norm_aliases:
            if n == q:
                bucket = 0
                sim = 1.0
            elif n.startswith(q):
                bucket = 1
                sim = 0.98
            elif any(part.startswith(q) for part in n.split()):
                bucket = 2
                sim = 0.95
            elif q in n:
                bucket = 3
                sim = 0.90
            else:
                sim = difflib.SequenceMatcher(None, q, n).ratio()
                if sim < 0.45:
                    continue
                bucket = 4

            if bucket < best_bucket or (bucket == best_bucket and sim > best_sim):
                best_bucket = bucket
                best_sim = sim

        if best_bucket != 99:
            scored.append((best_bucket, -best_sim, name))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    ranked = [name for _, _, name in scored]
    return ranked[:limit]


def _rank_text_options(options: list[str], query: str) -> list[str]:
    """Rank generic text options for search-box suggestions."""
    if not options:
        return []
    q = (query or "").strip()
    if not q:
        return options
    return _rank_fighter_options(options, q, alias_map=None, limit=max(len(options), 1))


@st.cache_data(ttl=86400, show_spinner=False)
def _geocode_location_cached(location: str) -> tuple[float, float] | None:
    """Best-effort geocode via Nominatim (OpenStreetMap)."""
    query = (location or "").strip()
    if not query:
        return None
    try:
        url = (
            "https://nominatim.openstreetmap.org/search"
            f"?q={quote_plus(query)}&format=json&limit=1"
        )
        req = request.Request(
            url,
            headers={"User-Agent": "FightProphet/1.0 (events-map)"},
        )
        with request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not payload:
            return None
        lat = float(payload[0]["lat"])
        lon = float(payload[0]["lon"])
        return (lat, lon)
    except Exception:
        return None


def _render_events_location_map(
    df_all_events: pd.DataFrame,
    selected_event: str,
    *,
    max_locations: int = 40,
) -> None:
    """Render an event map aggregated by distinct location for lighter reruns."""
    if df_all_events.empty or "location" not in df_all_events.columns or "event_name" not in df_all_events.columns:
        return

    map_source = df_all_events[["event_name", "location"]].copy()
    if "event_date" in df_all_events.columns:
        map_source["event_date"] = pd.to_datetime(df_all_events["event_date"], errors="coerce")
    else:
        map_source["event_date"] = pd.NaT
    map_source["event_name"] = map_source["event_name"].astype(str).str.strip()
    map_source["location"] = map_source["location"].astype(str).str.strip()
    map_source = map_source[
        (map_source["event_name"] != "")
        & (map_source["location"] != "")
        & (~map_source["location"].str.lower().isin({"nan", "nat", "none"}))
    ].drop_duplicates()
    if map_source.empty:
        return

    selected_location = ""
    selected_match = map_source[map_source["event_name"] == str(selected_event)]
    if not selected_match.empty:
        selected_location = str(selected_match.iloc[0]["location"]).strip()

    per_location = (
        map_source.groupby("location", as_index=False)
        .agg(
            events_count=("event_name", "nunique"),
            latest_event_date=("event_date", "max"),
        )
    )
    latest_rows = (
        map_source.sort_values("event_date", ascending=False, na_position="last")
        .drop_duplicates(subset=["location"], keep="first")
        [["location", "event_name"]]
        .rename(columns={"event_name": "latest_event"})
    )
    per_location = per_location.merge(latest_rows, on="location", how="left")
    per_location = per_location.sort_values("latest_event_date", ascending=False, na_position="last")

    total_locations = len(per_location)
    max_locations = max(5, int(max_locations))
    if total_locations > max_locations:
        trimmed = per_location.head(max_locations).copy()
        if selected_location:
            selected_row = per_location[per_location["location"] == selected_location].head(1)
            if not selected_row.empty and selected_location not in set(trimmed["location"].astype(str).tolist()):
                trimmed = pd.concat([trimmed, selected_row], ignore_index=True)
                trimmed = trimmed.drop_duplicates(subset=["location"], keep="first")
        per_location = trimmed
        st.caption(f"Showing {len(per_location):,} of {total_locations:,} locations for faster rendering.")

    map_rows: list[dict[str, object]] = []
    for _, row in per_location.iterrows():
        loc = str(row["location"])
        coords = _geocode_location_cached(loc)
        if coords is None:
            continue
        lat, lon = coords
        n_events = int(row.get("events_count", 1) or 1)
        map_rows.append(
            {
                "location": loc,
                "lat": lat,
                "lon": lon,
                "events_in_timeframe": n_events,
                "latest_event": str(row.get("latest_event", "")),
                "legend_label": (f"★ {loc}" if selected_location and loc == selected_location else loc),
            }
        )

    if not map_rows:
        return

    map_df = pd.DataFrame(map_rows)
    map_df["marker_size"] = map_df["events_in_timeframe"].clip(lower=1, upper=20)
    if selected_location:
        map_df.loc[map_df["location"] == selected_location, "marker_size"] = (
            map_df.loc[map_df["location"] == selected_location, "marker_size"].clip(lower=6) + 3
        )

    try:
        center_lat = float(map_df["lat"].mean())
        center_lon = float(map_df["lon"].mean())
        fig_map = px.scatter_mapbox(
            map_df,
            lat="lat",
            lon="lon",
            hover_name="location",
            hover_data={
                "events_in_timeframe": True,
                "latest_event": True,
                "lat": ":.3f",
                "lon": ":.3f",
            },
            labels={
                "events_in_timeframe": "Events in timeframe",
                "latest_event": "Latest event",
            },
            color="legend_label",
            size="marker_size",
            size_max=24,
            zoom=1.2,
            center={"lat": center_lat, "lon": center_lon},
            mapbox_style="carto-darkmatter",
        )
        fig_map.update_traces(marker=dict(opacity=0.82, line=dict(width=1, color="#ffffff")))
        fig_map.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=430,
            margin=dict(l=0, r=0, t=38, b=0),
            title="Event location map",
            legend=dict(title="Locations", orientation="v", y=1.0, x=1.01, xanchor="left"),
        )
    except Exception:
        fig_map = px.scatter_geo(
            map_df,
            lat="lat",
            lon="lon",
            hover_name="location",
            hover_data={
                "events_in_timeframe": True,
                "latest_event": True,
                "lat": ":.3f",
                "lon": ":.3f",
            },
            labels={
                "events_in_timeframe": "Events in timeframe",
                "latest_event": "Latest event",
            },
            color="legend_label",
            size="marker_size",
            size_max=20,
            projection="natural earth",
        )
        fig_map.update_traces(marker=dict(line=dict(width=1, color="#ffffff")))
        fig_map.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=390,
            margin=dict(l=10, r=10, t=40, b=10),
            title="Event location map",
            legend=dict(title="Locations"),
            geo=dict(showland=True, landcolor="rgba(63,63,70,0.45)", showcountries=True),
        )
    st.plotly_chart(fig_map, width="stretch")


# ---------------------------------------------------------------------------
# Page: Home
# ---------------------------------------------------------------------------


def _ui_copy_pack() -> dict[str, object]:
    lang = _normalize_lang(st.session_state.get("ui_lang", "en")) or "en"
    packs: dict[str, dict[str, object]] = {
        "en": {
            "hero_title": "Fight Prophet",
        "hero_sub": "Fight Prophet makes MMA analytics easy to read: upcoming picks, model diagnostics, rankings, belt holders, and fighter cards in one place.",
            "hero_badge": "For information and education only — never financial advice.",
            "tabs": ["How to use", "Model engine", "Risk controls"],
            "how_intro": "Simple workflow for new users:",
            "how_steps": [
                "Start in **Upcoming Predictions** to compare model probability vs market probability.",
                "Open **Fight Lab** to review historical outcomes plus metrics like accuracy, F1, AUC, Brier, and log loss.",
                "Use **Fighter Rankings** and **Fighter Cards** to understand matchup context.",
                "Use **Belt Holders** to track champions, title-fight history, and manual vacate overrides.",
            ],
            "engine_intro": "Fight Prophet serves three ML views for decision support:",
            "engine_points": [
                "**Ensemble**: combines multiple learners to reduce single-model bias.",
                "**CatBoost**: gradient boosting for structured/tabular MMA features.",
                "**LogReg**: interpretable baseline that stabilizes directional probability checks.",
                "Signals are generated from historical outcomes and feature engineering — outputs are probabilistic, not guarantees.",
            ],
            "model_picker_intro": "Available model views:",
            "model_picker_recommendation": "Recommendation: start with **CatBoost** for the best overall default view.",
            "risk_intro": "Responsible-use guardrails:",
            "risk_points": [
                "No guaranteed outcomes, no lock picks, no sure-profit claims.",
                "Content is informational; users remain fully responsible for betting decisions.",
                "If betting is illegal or restricted in your location, do not use outputs for wagering.",
                "If you feel loss-chasing behavior, stop immediately and seek local gambling support resources.",
            ],
            "story_title": "Built by fans, improved by community",
            "story_text": "Fight Prophet is open source. Community members can contribute by reporting data issues, suggesting features, improving model diagnostics, and helping maintain manual override datasets (like title vacates and fighter countries).",
            "quick_stats": "Platform snapshot",
            "cards": ["Upcoming Fights", "Tracked Events", "Historical Picks", "Model Variants"],
            "cards_note": "Counts refresh from current Parquet exports.",
            "cta": "Explore with the sidebar, compare evidence across pages, and contribute improvements back to the community.",
            "feedback": "See room for improvement? Open an issue on GitHub or comment on LinkedIn so the community can improve the models and data.",
        },
        "es": {
            "hero_title": "Fight Prophet",
            "hero_sub": "Fight Prophet facilita la analítica de MMA: picks próximos, diagnóstico de modelos, rankings, cinturones y tarjetas de peleadores en un solo lugar.",
            "hero_badge": "Solo para información y educación — nunca asesoría financiera.",
            "tabs": ["Cómo usar", "Motor del modelo", "Controles de riesgo"],
            "how_intro": "Flujo simple para nuevos usuarios:",
            "how_steps": [
                "Ve a **Próximas Predicciones** y compara probabilidad del modelo vs probabilidad de mercado.",
                "Abre **Fight Lab** para revisar histórico y métricas (accuracy, F1, AUC, Brier y log loss).",
                "Usa **Rankings** y **Tarjetas de Peleadores** para contexto del combate.",
                "Usa **Poseedores del Cinturón** para campeones, historial titular y vacantes manuales.",
            ],
            "engine_intro": "Fight Prophet muestra tres vistas de ML para soporte de decisiones:",
            "engine_points": [
                "**Ensemble**: combina varios modelos para reducir sesgo de un solo modelo.",
                "**CatBoost**: gradient boosting para variables tabulares de MMA.",
                "**LogReg**: baseline interpretable para validar dirección de probabilidades.",
                "Las señales son probabilísticas, no garantías de resultado.",
            ],
            "model_picker_intro": "Modelos disponibles:",
            "model_picker_recommendation": "Recomendación: empieza con **CatBoost** como vista general por defecto.",
            "risk_intro": "Reglas de uso responsable:",
            "risk_points": [
                "Sin resultados garantizados, sin picks seguros, sin promesas de ganancia.",
                "El contenido es informativo; tú asumes toda la responsabilidad.",
                "Si apostar es ilegal o restringido en tu jurisdicción, no uses salidas para apostar.",
                "Si detectas persecución de pérdidas, detente y busca ayuda local.",
            ],
            "story_title": "Hecho por fans, mejorado por la comunidad",
            "story_text": "Fight Prophet es open source. La comunidad puede contribuir reportando errores de datos, proponiendo funciones, mejorando métricas del modelo y manteniendo datasets manuales (vacantes y países).",
            "quick_stats": "Resumen de plataforma",
            "cards": ["Peleas Próximas", "Eventos", "Picks Históricos", "Modelos"],
            "cards_note": "Los conteos se actualizan desde los Parquet actuales.",
            "cta": "Explora con la barra lateral, contrasta evidencia entre páginas y aporta mejoras a la comunidad.",
            "feedback": "¿Ves algo para mejorar? Abre un issue en GitHub o comenta en LinkedIn para que la comunidad mejore modelos y datos.",
        },
        "pt": {
            "hero_title": "Fight Prophet",
            "hero_sub": "Fight Prophet deixa a análise de MMA fácil de entender: previsões, diagnóstico de modelo, rankings, cinturões e cards de lutadores em um só lugar.",
            "hero_badge": "Somente para informação e educação — nunca aconselhamento financeiro.",
            "tabs": ["Como usar", "Motor do modelo", "Controles de risco"],
            "how_intro": "Fluxo simples para novos usuários:",
            "how_steps": [
                "Vá em **Próximas Previsões** e compare probabilidade do modelo vs mercado.",
                "Abra o **Fight Lab** para histórico + métricas (acurácia, F1, AUC, Brier e log loss).",
                "Use **Rankings** e **Cards de Lutadores** para contexto da luta.",
                "Use **Detentores do Cinturão** para campeões, histórico de lutas por título e vacâncias manuais.",
            ],
            "engine_intro": "Fight Prophet oferece três visões de ML para suporte à decisão:",
            "engine_points": [
                "**Ensemble**: combina modelos para reduzir viés de um único algoritmo.",
                "**CatBoost**: gradient boosting para features tabulares de MMA.",
                "**LogReg**: baseline interpretável para checar direção das probabilidades.",
                "Os sinais são probabilísticos e não garantem resultados.",
            ],
            "model_picker_intro": "Modelos disponíveis:",
            "model_picker_recommendation": "Recomendação: comece com **CatBoost** como melhor visão padrão.",
            "risk_intro": "Regras de uso responsável:",
            "risk_points": [
                "Sem resultados garantidos, sem picks certos, sem promessa de lucro.",
                "Conteúdo informativo; a responsabilidade pela aposta é do usuário.",
                "Se apostar for ilegal ou restrito na sua jurisdição, não use as saídas para apostar.",
                "Se houver comportamento de recuperar perdas, pare e busque apoio local.",
            ],
            "story_title": "Feito por fãs, evoluído pela comunidade",
            "story_text": "Fight Prophet é open source. A comunidade pode contribuir com correções de dados, novas funcionalidades, melhorias de métricas e manutenção de datasets manuais (vacâncias e países).",
            "quick_stats": "Visão rápida da plataforma",
            "cards": ["Lutas Próximas", "Eventos", "Picks Históricos", "Modelos"],
            "cards_note": "As contagens atualizam pelos exports Parquet atuais.",
            "cta": "Use a barra lateral para explorar, comparar evidências entre páginas e contribuir com a comunidade.",
            "feedback": "Viu espaço para melhorar? Abra uma issue no GitHub ou comente no LinkedIn para a comunidade evoluir modelos e dados.",
        },
    }
    return packs.get(lang, packs["en"])


def _render_model_picker_help() -> None:
    copy_pack = _ui_copy_pack()
    engine_points = copy_pack.get("engine_points", [])
    model_points = [str(point) for point in engine_points[:3]]
    intro = str(copy_pack.get("model_picker_intro", "Available model views:"))
    recommendation = str(
        copy_pack.get(
            "model_picker_recommendation",
            "Recommendation: start with **Ensemble** for the best overall default view.",
        )
    )
    st.caption("  \n".join([intro, *model_points, recommendation]))


def _terms_copy_pack() -> dict[str, object]:
    lang = _normalize_lang(st.session_state.get("ui_lang", "en")) or "en"
    packs: dict[str, dict[str, object]] = {
        "en": {
            "warning": "These terms improve safety posture but are not legal advice. Have licensed counsel review before production/commercial launch.",
            "core_title": "Core legal and risk terms",
            "core": [
                "Fight Prophet is an analytics product. It does not direct, instruct, or guarantee any bet, stake size, or betting outcome.",
                "All probabilities, rankings, and signals are informational and educational only.",
                "No financial, investment, legal, or gambling advice is provided.",
                "Predictions are data-based estimates and may be wrong, delayed, incomplete, or unavailable.",
                "Users must independently verify odds, injuries, bout changes, and local legality before acting.",
                "You accept full responsibility for decisions, losses, taxes, penalties, and compliance obligations.",
                "To the maximum extent allowed by law, UpperCut Analytics and contributors disclaim liability for direct, indirect, incidental, special, exemplary, or consequential damages arising from use.",
                "No fiduciary, advisory, partnership, brokerage, or agency relationship is created by use of this dashboard.",
            ],
            "jur_title": "Jurisdiction notes (Las Vegas/Nevada, Colombia, Brazil)",
            "jur_intro": "Use is permitted only where lawful. You are solely responsible for local compliance and age eligibility.",
            "jur_labels": ["Las Vegas / Nevada (US)", "Colombia", "Brazil"],
            "jur_points": [
                [
                    "This product is not a sportsbook, bookmaker, or wagering operator.",
                    "Nothing here should be interpreted as regulated gaming advice or solicitation.",
                    "Users in Nevada must follow all applicable state/federal gaming laws and platform terms of licensed operators.",
                ],
                [
                    "No representation is made that betting is legal for every user or channel in Colombia.",
                    "Users must comply with all applicable Colombian rules and only use duly authorized operators where required.",
                    "Outputs are informational analytics, not a directive to place a wager.",
                ],
                [
                    "No representation is made that wagering is legal in all contexts for Brazilian users.",
                    "Users must verify current Brazilian law, operator authorization, and tax/reporting obligations before any action.",
                    "Outputs are informational analytics and not betting instructions.",
                ],
            ],
            "safe_title": "User safety commitments",
            "safe_points": [
                "Do not bet money you cannot afford to lose.",
                "Use strict loss limits and time limits.",
                "Do not chase losses or treat model outputs as certainty.",
                "If risk behavior appears, stop and seek local responsible-gambling support.",
            ],
        },
        "es": {
            "warning": "Estos términos mejoran la protección, pero no son asesoría legal. Revisión recomendada por abogado licenciado antes de uso comercial.",
            "core_title": "Términos legales y de riesgo",
            "core": [
                "Fight Prophet es un producto analítico; no ordena, instruye ni garantiza apuestas o resultados.",
                "Probabilidades, rankings y señales son solo informativos y educativos.",
                "No se ofrece asesoría financiera, legal, de inversión ni de apuestas.",
                "Las predicciones son estimaciones basadas en datos y pueden fallar o estar incompletas.",
                "El usuario debe verificar cuotas, lesiones, cambios de cartelera y legalidad local.",
                "Toda decisión, pérdida y cumplimiento normativo es responsabilidad exclusiva del usuario.",
                "En la máxima medida permitida por la ley, UpperCut Analytics y colaboradores excluyen responsabilidad por daños derivados del uso.",
                "El uso no crea relación fiduciaria, asesoría, corretaje ni representación.",
            ],
            "jur_title": "Notas por jurisdicción (Las Vegas/Nevada, Colombia, Brasil)",
            "jur_intro": "Solo usar donde sea legal. El usuario es responsable del cumplimiento local y edad mínima.",
            "jur_labels": ["Las Vegas / Nevada (EE. UU.)", "Colombia", "Brasil"],
            "jur_points": [
                [
                    "Este producto no es casa de apuestas ni operador de juego.",
                    "No debe interpretarse como asesoría regulada ni solicitud de apuesta.",
                    "En Nevada, el usuario debe cumplir leyes aplicables y términos de operadores autorizados.",
                ],
                [
                    "No se garantiza que apostar sea legal para todos los usuarios o canales en Colombia.",
                    "El usuario debe cumplir la regulación colombiana aplicable y usar operadores autorizados cuando corresponda.",
                    "Las salidas son analítica informativa, no una orden de apostar.",
                ],
                [
                    "No se garantiza legalidad universal de apuestas para todos los contextos en Brasil.",
                    "El usuario debe validar ley vigente, autorización del operador y obligaciones fiscales.",
                    "Las salidas son analítica informativa y no instrucciones de apuesta.",
                ],
            ],
            "safe_title": "Compromisos de seguridad del usuario",
            "safe_points": [
                "No apuestes dinero que no puedas perder.",
                "Usa límites de pérdida y de tiempo.",
                "No persigas pérdidas ni tomes los modelos como certeza.",
                "Si detectas conducta de riesgo, detente y busca apoyo local.",
            ],
        },
        "pt": {
            "warning": "Estes termos melhoram a proteção, mas não são aconselhamento jurídico. Recomenda-se revisão por advogado habilitado antes do uso comercial.",
            "core_title": "Termos legais e de risco",
            "core": [
                "Fight Prophet é um produto analítico; não ordena, instrui ou garante apostas/resultados.",
                "Probabilidades, rankings e sinais são apenas informativos e educacionais.",
                "Não há aconselhamento financeiro, jurídico, de investimento ou apostas.",
                "As previsões são estimativas baseadas em dados e podem falhar ou estar incompletas.",
                "O usuário deve validar odds, lesões, mudanças de card e legalidade local.",
                "Toda decisão, perda e obrigação regulatória é responsabilidade exclusiva do usuário.",
                "Na máxima extensão permitida em lei, UpperCut Analytics e colaboradores excluem responsabilidade por danos decorrentes do uso.",
                "O uso não cria relação fiduciária, consultiva, corretagem ou representação.",
            ],
            "jur_title": "Notas de jurisdição (Las Vegas/Nevada, Colômbia, Brasil)",
            "jur_intro": "Use apenas onde for legal. O usuário responde por conformidade local e idade mínima.",
            "jur_labels": ["Las Vegas / Nevada (EUA)", "Colômbia", "Brasil"],
            "jur_points": [
                [
                    "Este produto não é casa de apostas nem operador de jogo.",
                    "Nada aqui deve ser entendido como aconselhamento regulado ou solicitação de aposta.",
                    "Usuários em Nevada devem cumprir leis aplicáveis e termos de operadores licenciados.",
                ],
                [
                    "Não há garantia de legalidade de apostas para todos os usuários/canais na Colômbia.",
                    "O usuário deve cumprir regras colombianas aplicáveis e usar operadores autorizados quando exigido.",
                    "As saídas são analíticas informativas, não instruções de aposta.",
                ],
                [
                    "Não há garantia de legalidade universal de apostas para todos os contextos no Brasil.",
                    "O usuário deve verificar lei vigente, autorização do operador e obrigações tributárias.",
                    "As saídas são analíticas informativas, não instruções de aposta.",
                ],
            ],
            "safe_title": "Compromissos de segurança do usuário",
            "safe_points": [
                "Não aposte dinheiro que você não pode perder.",
                "Use limites rígidos de perda e de tempo.",
                "Não persiga perdas nem trate os modelos como certeza.",
                "Se notar comportamento de risco, pare e busque apoio local.",
            ],
        },
    }
    return packs.get(lang, packs["en"])


def page_home() -> None:
    """Home page — redirects users to the Astro landing site for full experience."""
    st.markdown(
        """
        <style>
            .fp-redirect-wrap {
                text-align: center;
                padding: 2rem 1rem;
            }
            .fp-redirect-wrap h2 {
                color: #fef2f2;
                margin-bottom: 0.5rem;
            }
            .fp-redirect-wrap p {
                color: #d4d4d8;
                font-size: 1.05rem;
                line-height: 1.6;
                max-width: 60ch;
                margin: 0 auto 1rem;
            }
            .fp-redirect-wrap a {
                display: inline-block;
                background: linear-gradient(160deg, #ef4444, #dc2626);
                color: #ffffff;
                border-radius: 0.65rem;
                padding: 0.6rem 1.2rem;
                font-weight: 700;
                text-decoration: none;
                box-shadow: 0 0 14px rgba(239,68,68,0.35);
            }
        </style>
        <div class="fp-redirect-wrap">
            <h2>Welcome to Fight Prophet</h2>
            <p>
                <strong>The fight data already knows who's getting smashed. We just highlight it.</strong><br /><br />
                Fight Prophet makes MMA analytics easy to read: upcoming picks, model diagnostics,
                rankings, belt holders, and fighter cards in one place.
                <br /><br />
                The home page has moved to
                <a href="https://fightprophet.com" target="_blank" rel="noopener noreferrer">fightprophet.com</a>.
                Use the sidebar to navigate directly to Upcoming Predictions, Fight Lab, Rankings, and more.
            </p>
            <a href="https://fightprophet.com" target="_blank" rel="noopener noreferrer">Visit fightprophet.com</a>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# (Legacy page_home CSS and markup removed — content migrated to Astro site)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Page: Terms & Conditions
# ---------------------------------------------------------------------------


def page_terms() -> None:
    lang = _normalize_lang(st.session_state.get("ui_lang", "en")) or "en"
    redirect_title_map = {
        "en": "Terms now live on the main Fight Prophet site.",
        "es": "Los términos ahora viven en el sitio principal de Fight Prophet.",
        "pt": "Os termos agora ficam no site principal do Fight Prophet.",
    }
    redirect_body_map = {
        "en": "Redirecting you to the Astro terms page in the same tab so the experience stays consistent.",
        "es": "Te estamos redirigiendo a la página de términos en Astro en la misma pestaña para mantener la experiencia consistente.",
        "pt": "Estamos te redirecionando para a página de termos em Astro na mesma aba para manter a experiência consistente.",
    }
    redirect_cta_map = {
        "en": "Open terms on fightprophet.com",
        "es": "Abrir términos en fightprophet.com",
        "pt": "Abrir termos em fightprophet.com",
    }

    st.header(f"{t('page.terms.title')}")
    st.info(redirect_title_map.get(lang, redirect_title_map["en"]))
    st.caption(redirect_body_map.get(lang, redirect_body_map["en"]))
    st.markdown(
        f'<a class="fp-sidebar-nav-link fp-site-shell-link" href="{escape(_MARKETING_TERMS_URL, quote=True)}">'
        f'<span class="fp-sidebar-nav-copy"><span class="fp-sidebar-nav-icon fp-sidebar-nav-icon--terms" aria-hidden="true"></span>'
        f'<span>{escape(redirect_cta_map.get(lang, redirect_cta_map["en"]))}</span></span></a>',
        unsafe_allow_html=True,
    )
    _st_components.html(
        f"""
        <script>
          (function() {{
            try {{
              var win = window.parent || window;
              var doc = win.document;
              var overlay = doc && doc.getElementById('fp-site-handoff');
              if (overlay) {{
                overlay.hidden = false;
                overlay.setAttribute('aria-hidden', 'false');
                win.requestAnimationFrame(function() {{
                  overlay.dataset.visible = 'true';
                }});
              }}
              win.setTimeout(function() {{
                win.location.href = {json.dumps(_MARKETING_TERMS_URL)};
              }}, 180);
            }} catch (e) {{}}
          }})();
        </script>
        """,
        height=0,
    )


# ---------------------------------------------------------------------------
# Page: Upcoming Predictions
# ---------------------------------------------------------------------------


def page_upcoming() -> None:
    st.header(f"{t('page.upcoming.title')}")

    model_view = st.selectbox(
        t("page.upcoming.prediction_model"),
        ["CatBoost", "Ensemble", "LogReg"],
        index=0,
    )
    _render_model_picker_help()

    folder_map = {
        "Ensemble": FOLDER_UPCOMING_ENSEMBLE,
        "CatBoost": FOLDER_UPCOMING_CATBOOST,
        "LogReg": FOLDER_UPCOMING_LOGREG,
    }
    stats_folder_map = {
        "Ensemble": FOLDER_STATS_ENSEMBLE,
        "CatBoost": FOLDER_STATS_CATBOOST,
        "LogReg": FOLDER_STATS_LOGREG,
    }

    selected_folder = folder_map.get(model_view, FOLDER_UPCOMING_CATBOOST)
    df_upcoming = _load_prepared_upcoming_cards(selected_folder, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    df_stats = _read_parquet(
        stats_folder_map.get(model_view, FOLDER_STATS_CATBOOST),
        ACTIVE_PARQUET_BASE,
        ACTIVE_PREFIX,
    )

    if df_upcoming.empty:
        st.info(t("page.upcoming.no_data"))
        return

    next_event_name = str(df_upcoming.iloc[0].get("event_name", "") or "").strip()
    if not next_event_name:
        st.info(t("page.upcoming.no_data"))
        return

    closest_event_label = t("page.upcoming.closest_event")
    all_events_label = t("page.upcoming.all_events")
    event_names = [
        name
        for name in dict.fromkeys(df_upcoming["event_name"].dropna().astype(str).tolist())
        if str(name).strip()
    ]
    event_picker_options = [closest_event_label, all_events_label] + event_names
    selected_event = st.selectbox(t("page.upcoming.filter_event"), event_picker_options, index=0)

    if selected_event == closest_event_label:
        st.caption(t("page.upcoming.showing_next_event"))
        df_show = df_upcoming[df_upcoming["event_name"] == next_event_name].copy()
    elif selected_event == all_events_label:
        df_show = df_upcoming.copy()
    else:
        df_show = df_upcoming[df_upcoming["event_name"] == selected_event].copy()

    analyzed_fights_total = None
    if not df_stats.empty:
        analyzed_fights_total = df_stats.iloc[0].get("total_fights")
    analyzed_fights_display = "—"
    if analyzed_fights_total is not None and not pd.isna(analyzed_fights_total):
        try:
            analyzed_fights_display = f"{int(float(analyzed_fights_total)):,}"
        except Exception:
            analyzed_fights_display = str(analyzed_fights_total)
    fights_value_html = (
        "<div class='kpi-card-value--stack'>"
        f"<div class='kpi-card-value-line'><strong>{len(df_show):,}</strong><span>{escape(t('page.upcoming.upcoming_fights'))}</span></div>"
        f"<div class='kpi-card-value-line'><strong>{escape(analyzed_fights_display)}</strong><span>{escape(t('page.upcoming.analyzed_fights'))}</span></div>"
        "</div>"
    )

    # Quick stats
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        _render_kpi_card(
            t("page.upcoming.total_fights"),
            fights_value_html,
            icon=_png_icon_html("b91c1c-fights-emoji.png", size=46, extra_class="fp-inline-emoji--kpi", label="Total fights")
            or _inline_emoji_html("⚔️", extra_class="fp-inline-emoji--kpi"),
            accent="#ef4444",
            value_is_html=True,
        )
    with col2:
        _render_kpi_card(
            t("page.upcoming.events"),
            str(df_show["event_name"].nunique()),
            icon=_png_icon_html("b91c1c-events-emoji-rail.png", size=46, extra_class="fp-inline-emoji--kpi", label="Events")
            or _inline_emoji_html("📅", extra_class="fp-inline-emoji--kpi"),
            accent="#22c55e",
        )
    strong = (df_show["signal_strength"] == "STRONG").sum() if "signal_strength" in df_show else 0
    with col3:
        _render_kpi_card(
            t("page.upcoming.strong_signals"),
            str(int(strong)),
            icon=_png_icon_html("b91c1c-signals-emoji.png", size=46, extra_class="fp-inline-emoji--kpi", label="Strong signals")
            or _inline_emoji_html("🟢", extra_class="fp-inline-emoji--kpi"),
            accent="#f59e0b",
        )
    recommended = df_show["recommended_bet"].sum() if "recommended_bet" in df_show else 0
    with col4:
        _render_kpi_card(
            t("page.upcoming.recommended_bets"),
            str(int(recommended)),
            icon=_png_icon_html("b91c1c-bets-emoji.png", size=46, extra_class="fp-inline-emoji--kpi", label="Recommended bets")
            or _inline_emoji_html("✅", extra_class="fp-inline-emoji--kpi"),
            accent="#8b5cf6",
        )

    _render_betting_signals_guide()

    st.divider()

    # Render fight cards
    for event_name, event_df in df_show.groupby("event_name", sort=False):
        event_row = event_df.iloc[0]
        event_date_text = "—"
        if "event_date" in event_row:
            _event_date = pd.to_datetime(event_row["event_date"], errors="coerce")
            if pd.notna(_event_date):
                event_date_text = _event_date.strftime("%Y-%m-%d")
        st.subheader(str(event_name))
        st.caption(f"{event_date_text} • {event_row.get('location', '—')}")

        for _, row in event_df.iterrows():
            _render_fight_card(row)

        st.divider()

    # Keep news in sidebar only to avoid duplicate headings in-page.


def _render_fight_card(row: pd.Series) -> None:
    """Render a single fight prediction card."""
    profile_stats_map = _fighter_card_stats_map(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    identity_map = _fighter_identity_map(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    signal = row.get("signal_strength", "")
    icon = _signal_icon(signal)
    bet_on = row.get("bet_on_name", "")
    model_prob = row.get("model_prob")
    market_prob = row.get("market_prob")
    edge = row.get("edge")
    recommended = row.get("recommended_bet", False)

    fighter = row["fighter_name_display"]
    opponent = row["opponent_name_display"]
    fighter_profile = profile_stats_map.get(_normalize_fighter_name_key(fighter), {})
    opponent_profile = profile_stats_map.get(_normalize_fighter_name_key(opponent), {})
    fighter_identity = identity_map.get(str(fighter).strip(), {})
    opponent_identity = identity_map.get(str(opponent).strip(), {})
    fighter_country = _value_from_candidates(row, ["fighter_country", "country", "fighter_country_code"])
    opponent_country = _value_from_candidates(row, ["opponent_country", "country", "opponent_country_code"])
    fighter_id_val = _value_from_candidates(row, ["fighter_id", "fighter_fighter_id", "fighter_id_display"])
    opponent_id_val = _value_from_candidates(row, ["opponent_id", "opponent_fighter_id", "opponent_id_display"])
    if fighter_country is None:
        fighter_country = fighter_profile.get("country")
    if opponent_country is None:
        opponent_country = opponent_profile.get("country")
    fighter_country_name = _resolve_fighter_country(fighter, fighter_id=fighter_id_val, country=fighter_country)
    opponent_country_name = _resolve_fighter_country(opponent, fighter_id=opponent_id_val, country=opponent_country)
    fighter_is_champ = _to_boolish(row.get("fighter_is_champion")) or _to_boolish(fighter_identity.get("is_champion"))
    opponent_is_champ = _to_boolish(row.get("opponent_is_champion")) or _to_boolish(opponent_identity.get("is_champion"))
    fighter_finish = _value_from_candidates(row, ["fighter_finish_rate", "finish_rate_win_shrunk", "finish_rate"])
    if fighter_finish is None or pd.isna(fighter_finish):
        fighter_finish = fighter_profile.get("finish_rate")
    fighter_sub = _value_from_candidates(row, ["fighter_sub_rate", "sub_rate_win_shrunk", "sub_rate"])
    if fighter_sub is None or pd.isna(fighter_sub):
        fighter_sub = fighter_profile.get("sub_rate")
    fighter_win_streak = _value_from_candidates(row, ["fighter_win_streak", "win_streak"])
    if fighter_win_streak is None or pd.isna(fighter_win_streak):
        fighter_win_streak = fighter_profile.get("win_streak")
    fighter_loss_streak = _value_from_candidates(row, ["fighter_loss_streak", "loss_streak"])
    if fighter_loss_streak is None or pd.isna(fighter_loss_streak):
        fighter_loss_streak = fighter_profile.get("loss_streak")
    fighter_wins = _value_from_candidates(row, ["fighter_wins", "wins"])
    if fighter_wins is None or pd.isna(fighter_wins):
        fighter_wins = fighter_profile.get("wins")
    fighter_losses = _value_from_candidates(row, ["fighter_losses", "losses"])
    if fighter_losses is None or pd.isna(fighter_losses):
        fighter_losses = fighter_profile.get("losses")
    fighter_weight = (
        _value_from_candidates(row, ["fighter_weight_class", "weight_class"])
        or fighter_profile.get("weight_class")
    )
    opponent_finish = _value_from_candidates(row, ["opponent_finish_rate", "opponent_finish_rate_win_shrunk"])
    if opponent_finish is None or pd.isna(opponent_finish):
        opponent_finish = opponent_profile.get("finish_rate")
    opponent_sub = _value_from_candidates(row, ["opponent_sub_rate", "opponent_sub_rate_win_shrunk"])
    if opponent_sub is None or pd.isna(opponent_sub):
        opponent_sub = opponent_profile.get("sub_rate")
    opponent_win_streak = _value_from_candidates(row, ["opponent_win_streak"])
    if opponent_win_streak is None or pd.isna(opponent_win_streak):
        opponent_win_streak = opponent_profile.get("win_streak")
    opponent_loss_streak = _value_from_candidates(row, ["opponent_loss_streak"])
    if opponent_loss_streak is None or pd.isna(opponent_loss_streak):
        opponent_loss_streak = opponent_profile.get("loss_streak")
    opponent_wins = _value_from_candidates(row, ["opponent_wins"])
    if opponent_wins is None or pd.isna(opponent_wins):
        opponent_wins = opponent_profile.get("wins")
    opponent_losses = _value_from_candidates(row, ["opponent_losses"])
    if opponent_losses is None or pd.isna(opponent_losses):
        opponent_losses = opponent_profile.get("losses")
    opponent_weight = (
        _value_from_candidates(row, ["opponent_weight_class", "weight_class"])
        or opponent_profile.get("weight_class")
    )
    fighter_name = "" if fighter is None else str(fighter).strip()
    opponent_name = "" if opponent is None else str(opponent).strip()
    bet_on_name = "" if bet_on is None else str(bet_on).strip()
    bet_on_fighter_side = bet_on_name == fighter_name
    likely_winner_name = fighter_name
    likely_winner_prob = model_prob
    if model_prob is not None and not pd.isna(model_prob) and float(model_prob) < 0.5:
        likely_winner_name = opponent_name
        likely_winner_prob = 1.0 - float(model_prob)

    fighter_prob = model_prob if model_prob is not None and not pd.isna(model_prob) else None
    opponent_prob = (1.0 - float(model_prob)) if fighter_prob is not None else None
    market_prob_bet_side = market_prob
    if market_prob is not None and not pd.isna(market_prob) and not bet_on_fighter_side:
        market_prob_bet_side = 1.0 - float(market_prob)

    fighter_href = _fighter_profile_href(fighter_name)
    opponent_href = _fighter_profile_href(opponent_name)
    likely_winner_label = _fighter_profile_link(likely_winner_name) if likely_winner_name else ""
    bet_on_label = _fighter_profile_link(bet_on_name) if bet_on_name else ""
    likely_winner_str = (
        f"{float(likely_winner_prob):.1%}"
        if likely_winner_prob is not None and not pd.isna(likely_winner_prob)
        else "—"
    )
    fighter_prob_str = f"{float(fighter_prob):.1%}" if fighter_prob is not None else "—"
    opponent_prob_str = f"{float(opponent_prob):.1%}" if opponent_prob is not None else "—"
    fighter_prob_width = max(0.0, min(float(fighter_prob or 0.0) * 100.0, 100.0))
    opponent_prob_width = max(0.0, min(float(opponent_prob or 0.0) * 100.0, 100.0))
    edge_pct = f"{edge:+.1%}" if edge is not None and not pd.isna(edge) else "—"
    market_str = (
        f"{float(market_prob_bet_side):.1%}"
        if market_prob_bet_side is not None and not pd.isna(market_prob_bet_side)
        else "—"
    )
    bet_side_odds = row.get("fighter_odds") if bet_on_fighter_side else row.get("opponent_odds")
    bet_side_market_prob = market_prob_bet_side if market_prob_bet_side is not None and not pd.isna(market_prob_bet_side) else None
    bet_on_is_underdog = False
    if bet_side_odds is not None and not pd.isna(bet_side_odds):
        try:
            bet_on_is_underdog = float(bet_side_odds) > 0
        except Exception:
            bet_on_is_underdog = False
    elif bet_side_market_prob is not None:
        bet_on_is_underdog = float(bet_side_market_prob) < 0.5

    signal_key = str(signal or "").strip().lower()
    signal_class = {
        "strong": "fp-matchup-signal--strong",
        "medium": "fp-matchup-signal--medium",
        "weak": "fp-matchup-signal--weak",
    }.get(signal_key, "fp-matchup-signal--neutral")
    signal_label = str(signal or "Signal").upper()
    recommended_chip = (
        " &nbsp;|&nbsp; "
        + (_png_icon_html("b91c1c-bets-emoji.png", size=16, extra_class="fp-inline-emoji--signal", label=t("page.upcoming.threshold_passed")) or _inline_emoji_html("✅", extra_class="fp-inline-emoji--signal"))
        + f" <b>{escape(t('page.upcoming.threshold_passed'))}</b>"
        if recommended
        else ""
    )
    value_label = (
        t("page.upcoming.underdog_value_angle")
        if bet_on_is_underdog
        else t("page.upcoming.best_value_bet")
    )
    matchup_label = str(row.get("weight_class") or fighter_weight or opponent_weight or "Fight matchup").strip()

    fighter_card_html = _build_fighter_card_html(
        name=fighter,
        country=fighter_country_name,
        weight_class=fighter_weight,
        is_champion=fighter_is_champ,
        finish_rate=fighter_finish,
        sub_rate=fighter_sub,
        win_streak=fighter_win_streak,
        loss_streak=fighter_loss_streak,
        wins=fighter_wins,
        losses=fighter_losses,
        compact=True,
        href=fighter_href,
    )
    opponent_card_html = _build_fighter_card_html(
        name=opponent,
        country=opponent_country_name,
        weight_class=opponent_weight,
        is_champion=opponent_is_champ,
        finish_rate=opponent_finish,
        sub_rate=opponent_sub,
        win_streak=opponent_win_streak,
        loss_streak=opponent_loss_streak,
        wins=opponent_wins,
        losses=opponent_losses,
        compact=True,
        href=opponent_href,
    )
    value_html = ""
    if bet_on_name:
        value_html = (
            f"<div class='fp-pick-value'>{icon} <b>{escape(value_label)}:</b> {bet_on_label}"
            f" &nbsp;|&nbsp; {escape(t('page.upcoming.edge'))}: <span style='font-family:ui-monospace,SFMono-Regular,monospace;'>{escape(edge_pct)}</span>"
            f" &nbsp;|&nbsp; {escape(t('page.upcoming.market'))}: <span style='font-family:ui-monospace,SFMono-Regular,monospace;'>{escape(market_str)}</span>"
            f" &nbsp;|&nbsp; {escape(t('page.upcoming.signal'))}: <b>{escape(signal_label)}</b>{recommended_chip}"
            "</div>"
        )

    st.markdown(
        _FIGHTER_CARD_CSS
        + _PREDICTION_MATCHUP_CSS
        + (
            f"<section class='fp-matchup-shell'>"
            "<div class='fp-matchup-head'>"
            "<div class='fp-matchup-head-copy'>"
            f"<div class='fp-matchup-eyebrow'>{escape(matchup_label)}</div>"
            "</div>"
            f"<div class='fp-matchup-signal {signal_class}'>{icon} {escape(signal_label)}</div>"
            "</div>"
            "<div class='fp-matchup-grid'>"
            "<div class='fp-fighter-pane'>"
            f"{fighter_card_html}"
            f"<div class='fp-odds-chip'><span class='fp-odds-chip-label'>Odds</span>{escape(_odds_display(row.get('fighter_odds')))}</div>"
            "</div>"
            "<div class='fp-versus-core'>"
            f"<div class='fp-versus-mark'>{_inline_emoji_html('⚔️', extra_class='fp-inline-emoji--versus')}</div>"
            f"<div class='fp-versus-title'>{escape(t('page.upcoming.model_probabilities'))}</div>"
            "<div class='fp-prob-stack'>"
            "<div class='fp-prob-row'>"
            f"<div class='fp-prob-meta'><span class='fp-prob-name'>{escape(fighter_name or 'Fighter')}</span><span class='fp-prob-value'>{escape(fighter_prob_str)}</span></div>"
            f"<div class='fp-prob-track'><div class='fp-prob-fill' style='width:{fighter_prob_width:.1f}%;'></div></div>"
            "</div>"
            "<div class='fp-prob-row'>"
            f"<div class='fp-prob-meta'><span class='fp-prob-name'>{escape(opponent_name or 'Opponent')}</span><span class='fp-prob-value'>{escape(opponent_prob_str)}</span></div>"
            f"<div class='fp-prob-track'><div class='fp-prob-fill fp-prob-fill--alt' style='width:{opponent_prob_width:.1f}%;'></div></div>"
            "</div>"
            "</div>"
            "</div>"
            "<div class='fp-fighter-pane'>"
            f"{opponent_card_html}"
            f"<div class='fp-odds-chip'><span class='fp-odds-chip-label'>Odds</span>{escape(_odds_display(row.get('opponent_odds')))}</div>"
            "</div>"
            "</div>"
            "<div class='fp-pick-summary'>"
            f"<div class='fp-pick-winner'><strong>{escape(t('page.upcoming.model_prediction_win'))}</strong> {likely_winner_label} <span class='fp-pick-confidence'>({escape(likely_winner_str)})</span></div>"
            f"{value_html}"
            f"<div class='fp-pick-note'>{escape(t('page.upcoming.value_signal_caption'))}</div>"
            "</div>"
            "</section>"
        ),
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Page: Model Performance
# ---------------------------------------------------------------------------


def page_model_performance() -> None:
    page_historical()


# ---------------------------------------------------------------------------
# Page: Historical Picks
# ---------------------------------------------------------------------------


def _render_model_diagnostics(model_view: str) -> None:
    """Render CatBoost feature importance + Optuna hparam importance / trials.

    Reads three dashboard parquets produced by mma_gold_catboost(_tune).py via
    mma_parquets_dashboard.py. All blocks degrade gracefully if a parquet is
    missing or empty (first runs, LogReg/Ensemble views).
    """
    with st.expander("Model Diagnostics — what the model leans on", expanded=False):
        st.caption(
            "Feature importance reflects the production CatBoost model. "
            "Hyperparameter importance comes from the latest Optuna study."
        )

        if model_view != "CatBoost":
            st.info("Diagnostics are currently published only for the CatBoost model.")
            return

        df_fi = _read_parquet(
            FOLDER_FEATURE_IMPORTANCE_CATBOOST,
            ACTIVE_PARQUET_BASE,
            ACTIVE_PREFIX,
        )
        st.subheader("Feature Importance")
        if df_fi.empty:
            st.info("No feature importance available yet — run the CatBoost trainer first.")
        else:
            df_fi = df_fi.copy()
            df_fi["importance"] = pd.to_numeric(df_fi["importance"], errors="coerce")
            df_fi = df_fi.dropna(subset=["importance"]).sort_values("importance", ascending=False)
            n_features = int(len(df_fi))
            top_n = st.slider(
                "Top features",
                min_value=5,
                max_value=max(5, min(60, n_features)),
                value=min(25, n_features),
                step=5,
                key="fi_top_n",
            )
            df_top = df_fi.head(top_n).iloc[::-1]
            colors = [
                "#ef4444" if k == "categorical" else "#3b82f6"
                for k in df_top.get("feature_kind", pd.Series(["numeric"] * len(df_top)))
            ]
            fig_fi = go.Figure()
            fig_fi.add_trace(
                go.Bar(
                    x=df_top["importance"],
                    y=df_top["feature"],
                    orientation="h",
                    marker=dict(color=colors),
                    hovertemplate="%{y}<br>importance: %{x:.3f}<extra></extra>",
                )
            )
            fig_fi.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=max(360, 22 * top_n),
                margin=dict(l=10, r=20, t=10, b=40),
                xaxis_title="PredictionValuesChange",
                yaxis_title="",
            )
            st.plotly_chart(fig_fi, use_container_width=True)
            st.caption(
                "Blue = numeric delta/profile feature, red = categorical (stance, weight class, …). "
                "Bars use CatBoost's PredictionValuesChange. Correlated features can split a single "
                "underlying signal across several rows — read directionally, not as ground truth."
            )

        df_hp = _read_parquet(
            FOLDER_HPARAM_IMPORTANCE_CATBOOST,
            ACTIVE_PARQUET_BASE,
            ACTIVE_PREFIX,
        )
        st.subheader("Optuna Hyperparameter Importance")
        if df_hp.empty:
            st.info("No Optuna study persisted yet — run the CatBoost tuner.")
        else:
            df_hp = df_hp.copy()
            df_hp["importance"] = pd.to_numeric(df_hp["importance"], errors="coerce")
            df_hp = df_hp.dropna(subset=["importance"]).sort_values("importance", ascending=True)
            fig_hp = go.Figure()
            fig_hp.add_trace(
                go.Bar(
                    x=df_hp["importance"],
                    y=df_hp["param"],
                    orientation="h",
                    marker=dict(color="#22c55e"),
                    hovertemplate="%{y}<br>importance: %{x:.3f}<extra></extra>",
                )
            )
            fig_hp.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=max(280, 28 * len(df_hp)),
                margin=dict(l=10, r=20, t=10, b=40),
                xaxis_title="fANOVA importance",
                yaxis_title="",
            )
            st.plotly_chart(fig_hp, use_container_width=True)
            n_trials_val = (
                int(df_hp["n_trials"].iloc[0])
                if "n_trials" in df_hp.columns and not df_hp["n_trials"].isna().all()
                else None
            )
            best_val = (
                float(df_hp["best_value"].iloc[0])
                if "best_value" in df_hp.columns and not df_hp["best_value"].isna().all()
                else None
            )
            if n_trials_val is not None and best_val is not None:
                st.caption(f"Latest study: {n_trials_val} trials, best valid score = {best_val:.4f}")

        df_tr = _read_parquet(
            FOLDER_TUNE_TRIALS_CATBOOST,
            ACTIVE_PARQUET_BASE,
            ACTIVE_PREFIX,
        )
        st.subheader("Optuna Trial History")
        if df_tr.empty:
            st.info("No tuning trial history available yet.")
        else:
            df_tr = df_tr.copy()
            df_tr["value"] = pd.to_numeric(df_tr["value"], errors="coerce")
            df_tr["best_value_so_far"] = pd.to_numeric(df_tr["best_value_so_far"], errors="coerce")
            df_tr = df_tr.sort_values("trial_number")
            fig_tr = go.Figure()
            fig_tr.add_trace(
                go.Scatter(
                    x=df_tr["trial_number"],
                    y=df_tr["value"],
                    mode="markers",
                    name="Trial score",
                    marker=dict(
                        size=8,
                        color=df_tr["value"],
                        colorscale="Viridis",
                        showscale=False,
                    ),
                    hovertemplate="trial %{x}<br>value: %{y:.4f}<extra></extra>",
                )
            )
            fig_tr.add_trace(
                go.Scatter(
                    x=df_tr["trial_number"],
                    y=df_tr["best_value_so_far"],
                    mode="lines",
                    name="Best so far",
                    line=dict(color="#ef4444", width=2),
                )
            )
            best_mask = df_tr.get("is_best", pd.Series([False] * len(df_tr))).fillna(False).astype(bool)
            if best_mask.any():
                fig_tr.add_trace(
                    go.Scatter(
                        x=df_tr.loc[best_mask, "trial_number"],
                        y=df_tr.loc[best_mask, "value"],
                        mode="markers",
                        name="Best trial",
                        marker=dict(size=14, color="#facc15", symbol="star"),
                        hovertemplate="best trial %{x}<br>value: %{y:.4f}<extra></extra>",
                    )
                )
            metric_label = (
                str(df_tr["metric"].iloc[0]).upper()
                if "metric" in df_tr.columns and not df_tr["metric"].isna().all()
                else "score"
            )
            fig_tr.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=380,
                margin=dict(l=10, r=20, t=10, b=40),
                xaxis_title="Trial",
                yaxis_title=metric_label,
                legend=dict(x=0.02, y=0.02),
            )
            st.plotly_chart(fig_tr, use_container_width=True)


def page_historical() -> None:
    st.header("Fight Lab: Historical Picks + Model Performance")

    model_view = st.selectbox(
        "Historical model",
        ["CatBoost", "Ensemble", "LogReg"],
        index=0,
    )
    _render_model_picker_help()

    hist_folder_map = {
        "Ensemble": FOLDER_HIST_ALL_ENSEMBLE,
        "CatBoost": FOLDER_HIST_ALL_CATBOOST,
        "LogReg": FOLDER_HIST_ALL_LOGREG,
    }
    stats_folder_map = {
        "Ensemble": FOLDER_STATS_ENSEMBLE,
        "CatBoost": FOLDER_STATS_CATBOOST,
        "LogReg": FOLDER_STATS_LOGREG,
    }
    cal_folder_map = {
        "Ensemble": FOLDER_CAL_ENSEMBLE,
        "CatBoost": FOLDER_CAL_CATBOOST,
        "LogReg": FOLDER_CAL_LOGREG,
    }

    df_hist = _read_parquet(
        hist_folder_map.get(model_view, FOLDER_HIST_ALL_CATBOOST),
        ACTIVE_PARQUET_BASE,
        ACTIVE_PREFIX,
    )
    if df_hist.empty:
        df_hist = _read_parquet(FOLDER_HIST_ALL, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    if df_hist.empty:
        st.info("No historical scored fights found.")
        return

    df_stats = _read_parquet(
        stats_folder_map.get(model_view, FOLDER_STATS_CATBOOST),
        ACTIVE_PARQUET_BASE,
        ACTIVE_PREFIX,
    )
    if df_stats.empty:
        df_stats = _read_parquet(FOLDER_STATS, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    if not df_stats.empty:
        row = df_stats.iloc[0]
        m1, m2, m3 = st.columns(3, gap="medium")
        with m1:
            _render_kpi_card(
                "Overall Accuracy",
                f"{float(row.get('accuracy', 0)):.1%}",
                icon=_png_icon_html("b91c1c-accuracy-emoji.png", size=46, extra_class="fp-inline-emoji--kpi", label="Overall accuracy")
                or _goat_icon_html(),
                accent="#22c55e",
            )
        with m2:
            _render_kpi_card(
                "Total Predictions",
                f"{int(float(row.get('total_fights', 0))):,}",
                icon=_png_icon_html("b91c1c-predictions-emoji-rail.png", size=52, extra_class="fp-inline-emoji--kpi fp-inline-emoji--kpi-heavy", label="Total predictions")
                or _goat_icon_html(),
                accent="#ef4444",
            )
        with m3:
            _render_kpi_card(
                "Events Covered",
                f"{int(float(row.get('events_covered', 0)))}",
                icon=_png_icon_html("b91c1c-events-emoji-rail.png", size=52, extra_class="fp-inline-emoji--kpi fp-inline-emoji--kpi-heavy", label="Events covered")
                or _goat_icon_html(),
                accent="#3b82f6",
            )

    if "event_date" in df_hist.columns:
        df_hist["event_date"] = pd.to_datetime(df_hist["event_date"], errors="coerce")
    if "edge" in df_hist.columns:
        df_hist["_edge_abs"] = pd.to_numeric(df_hist["edge"], errors="coerce").abs()
    else:
        df_hist["_edge_abs"] = pd.NA
    df_hist = df_hist.sort_values(
        by=["event_date", "_edge_abs"],
        ascending=[False, False],
        na_position="last",
    )

    total_available = len(df_hist)

    top_correct = df_hist["model_correct"].sum()
    top_total = len(df_hist)
    top_acc = top_correct / top_total if top_total > 0 else 0
    top_metrics = _compute_binary_metrics(df_hist)

    c1, _, c2 = st.columns([1, 0.08, 1], gap="medium")
    with c1:
        _render_kpi_card(
            "Correct Picks",
            f"{int(top_correct)}",
            icon=_png_icon_html("b91c1c-correct-emoji.png", size=46, extra_class="fp-inline-emoji--kpi", label="Correct picks")
            or _goat_icon_html(),
            accent="#10b981",
            compact=False,
        )
    with c2:
        _render_kpi_card(
            "Wrong Picks",
            f"{int(top_total - top_correct)}",
            icon=_png_icon_html("b91c1c-incorrect-emoji.png", size=46, extra_class="fp-inline-emoji--kpi", label="Wrong picks")
            or _goat_icon_html(),
            accent="#ef4444",
            compact=False,
        )

    st.markdown("<div style='height: 0.45rem;'></div>", unsafe_allow_html=True)

    _render_fighter_overview_card(
        [
            ("Accuracy", f"{top_acc:.1%}"),
            ("F1", f"{top_metrics['f1']:.3f}" if top_metrics["f1"] is not None else "—"),
            ("AUC", f"{top_metrics['auc']:.3f}" if top_metrics["auc"] is not None else "—"),
            ("Brier", f"{top_metrics['brier']:.3f}" if top_metrics["brier"] is not None else "—"),
            ("Log Loss", f"{top_metrics['log_loss']:.3f}" if top_metrics["log_loss"] is not None else "—"),
            ("Total Picks", f"{top_total:,}"),
        ],
        title="Historical Metrics",
        layout="strip",
    )

    st.divider()
    st.subheader("Calibration: Predicted vs Actual Win Rate")
    df_cal = _read_parquet(
        cal_folder_map.get(model_view, FOLDER_CAL_CATBOOST),
        ACTIVE_PARQUET_BASE,
        ACTIVE_PREFIX,
    )
    if df_cal.empty:
        df_cal = _read_parquet(FOLDER_CAL, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    if not df_cal.empty:
        cal_cols = ["prob_bucket", "n_fights", "actual_hit_rate"]
        df_cal = df_cal[[c for c in cal_cols if c in df_cal.columns]]
        if "prob_bucket" in df_cal.columns:
            df_cal = df_cal.sort_values("prob_bucket")

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=[0, 1],
                y=[0, 1],
                mode="lines",
                line=dict(dash="dash", color="#71717a", width=1),
                name="Perfect calibration",
                showlegend=True,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=df_cal["prob_bucket"],
                y=df_cal["actual_hit_rate"],
                mode="lines+markers",
                marker=dict(size=10, color="#ef4444"),
                line=dict(color="#ef4444", width=2),
                name="Model calibration",
                text=[f"n={n}" for n in df_cal["n_fights"]],
                hovertemplate="Bucket: %{x:.0%}<br>Actual: %{y:.1%}<br>%{text}<extra></extra>",
            )
        )
        fig.update_layout(
            xaxis_title="Predicted Probability Bucket",
            yaxis_title="Actual Hit Rate",
            xaxis=dict(tickformat=".0%", range=[0, 1]),
            yaxis=dict(tickformat=".0%", range=[0, 1]),
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=420,
            margin=dict(l=40, r=20, t=30, b=40),
            legend=dict(x=0.02, y=0.98),
        )
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Accuracy by Probability Bucket")
        df_cal_display = df_cal[["prob_bucket", "n_fights", "actual_hit_rate"]].copy()
        df_cal_display.columns = ["Prob Bucket", "# Fights", "Actual Hit Rate"]
        df_cal_display["Prob Bucket"] = df_cal_display["Prob Bucket"].apply(lambda x: f"{x:.0%}")
        df_cal_display["Actual Hit Rate"] = df_cal_display["Actual Hit Rate"].apply(lambda x: f"{x:.1%}")
        st.markdown(df_cal_display.to_html(index=False, escape=False), unsafe_allow_html=True)

    st.divider()
    _render_model_diagnostics(model_view)

    st.caption(f"Total available historical picks: {total_available:,}")

    slice_mode = st.radio(
        "Slice historical picks by",
        ["Count", "Date range"],
        horizontal=True,
        help="Filter the table and metrics by a recent-picks count or by event date interval.",
    )

    if slice_mode == "Count":
        count_options = sorted(
            {
                c
                for c in (
                    25,
                    50,
                    100,
                    200,
                    300,
                    500,
                    750,
                    1000,
                    2000,
                    5000,
                    total_available,
                )
                if c <= total_available
            }
        )
        default_count = min(200, total_available)
        selected_count = st.select_slider(
            "Number of most recent picks",
            options=count_options,
            value=default_count,
        )
        df_hist_view = df_hist.head(int(selected_count)).copy()
        window_label = f"Last {int(selected_count):,}"
    else:
        date_series = pd.to_datetime(df_hist["event_date"], errors="coerce")
        if date_series.notna().any():
            min_date = date_series.min().date()
            max_date = date_series.max().date()
            default_start = max(min_date, max_date - timedelta(days=365))
            selected_dates = st.date_input(
                "Event date range",
                value=(default_start, max_date),
                min_value=min_date,
                max_value=max_date,
                help="Shows picks whose event date falls inside this range.",
            )

            if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
                start_date, end_date = selected_dates
            else:
                start_date = end_date = selected_dates

            if start_date > end_date:
                start_date, end_date = end_date, start_date

            mask = date_series.dt.date.between(start_date, end_date, inclusive="both")
            df_hist_view = df_hist[mask].copy()
            window_label = f"{start_date.isoformat()} → {end_date.isoformat()}"
        else:
            st.info("No valid event dates found in this dataset; using all picks.")
            df_hist_view = df_hist.copy()
            window_label = "All"

    if df_hist_view.empty:
        st.info("No picks match the selected historical window.")
        return

    selected_correct = df_hist_view["model_correct"].sum()
    selected_total = len(df_hist_view)
    selected_acc = selected_correct / selected_total if selected_total > 0 else 0
    selected_metrics = _compute_binary_metrics(df_hist_view)
    st.caption(
        f"Selected window: {window_label} • Picks: {selected_total:,} • Accuracy: {selected_acc:.1%}"
    )
    selected_f1 = f"{selected_metrics['f1']:.3f}" if selected_metrics["f1"] is not None else "—"
    selected_auc = f"{selected_metrics['auc']:.3f}" if selected_metrics["auc"] is not None else "—"
    selected_brier = f"{selected_metrics['brier']:.3f}" if selected_metrics["brier"] is not None else "—"
    selected_log_loss = f"{selected_metrics['log_loss']:.3f}" if selected_metrics["log_loss"] is not None else "—"
    st.caption(
        f"Selected metrics • F1: {selected_f1} • AUC: {selected_auc} • Brier: {selected_brier} • LogLoss: {selected_log_loss}"
    )

    max_roll_window = max(5, min(100, selected_total))
    default_roll_window = min(20, max_roll_window)
    rolling_window = st.slider(
        "Rolling accuracy window size (fights)",
        min_value=5,
        max_value=max_roll_window,
        value=default_roll_window,
        step=1,
        help="Each chart point shows the average correctness over the previous N fights.",
    )

    # Accuracy over time
    st.subheader(f"Rolling Accuracy ({window_label}, window = {rolling_window} fights)")
    df_roll = df_hist_view.sort_values("event_date").copy()
    min_periods = min(5, rolling_window)
    df_roll["rolling_acc"] = (
        df_roll["model_correct"].rolling(rolling_window, min_periods=min_periods).mean()
    )

    fig_roll = px.line(
        df_roll,
        x="event_date",
        y="rolling_acc",
        labels={"event_date": "Event Date", "rolling_acc": "Rolling Accuracy"},
        template="plotly_dark",
    )
    fig_roll.update_traces(line_color="#ef4444")
    fig_roll.add_hline(y=0.5, line_dash="dash", line_color="#71717a", annotation_text="50%")
    fig_roll.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(tickformat=".0%", range=[0, 1]),
        height=400,
        margin=dict(l=40, r=20, t=30, b=40),
    )
    st.plotly_chart(fig_roll, use_container_width=True)

    st.divider()

    # Detailed table
    st.subheader("Fight-by-Fight Results")

    # Add visual result column
    df_display = df_hist_view.copy()
    df_display["Fighter Country"] = df_display.apply(
        lambda r: _resolve_fighter_country(
            r.get("fighter_name_display"),
            fighter_id=_value_from_candidates(r, ["fighter_id", "fighter_fighter_id", "fighter_id_display"]),
            country=_value_from_candidates(r, ["fighter_country", "country", "fighter_country_code"]),
        ) or "N/A",
        axis=1,
    )
    df_display["Opponent Country"] = df_display.apply(
        lambda r: _resolve_fighter_country(
            r.get("opponent_name_display"),
            fighter_id=_value_from_candidates(r, ["opponent_id", "opponent_fighter_id", "opponent_id_display"]),
            country=_value_from_candidates(r, ["opponent_country", "country", "opponent_country_code"]),
        ) or "N/A",
        axis=1,
    )
    df_display["Fighter Badge"] = df_display.apply(lambda r: _fighter_badge_from_row(r, "fighter"), axis=1)
    df_display["Opponent Badge"] = df_display.apply(lambda r: _fighter_badge_from_row(r, "opponent"), axis=1)
    df_display["Result"] = df_display["model_correct"].map(
        {
            1: "<span class='result-badge result-win'>Correct</span>",
            0: "<span class='result-badge result-loss'>Wrong</span>",
        }
    )
    df_display["Model Prob"] = df_display["model_prob"].apply(
        lambda x: f"{x:.1%}" if pd.notna(x) else "—"
    )
    df_display["Edge"] = df_display["edge"].apply(
        lambda x: f"{x:+.1%}" if pd.notna(x) else "—"
    )

    cols_show = [
        "Fighter Badge",
        "event_date",
        "fighter_name_display",
        "Fighter Country",
        "Opponent Badge",
        "opponent_name_display",
        "Opponent Country",
        "bet_on_name",
        "winner_name_display",
        "Result",
        "Model Prob",
        "Edge",
        "signal_strength",
    ]
    cols_show = [c for c in cols_show if c in df_display.columns]
    df_display = df_display[cols_show].rename(
        columns={
            "Fighter Badge": "Ftr",
            "event_date": "Date",
            "fighter_name_display": "Fighter",
            "Fighter Country": "Fighter Country",
            "Opponent Badge": "Opp",
            "opponent_name_display": "Opponent",
            "Opponent Country": "Opponent Country",
            "bet_on_name": "Bet On",
            "winner_name_display": "Winner",
            "signal_strength": "Signal",
        }
    )
    for link_col in ["Fighter", "Opponent", "Bet On", "Winner"]:
        if link_col in df_display.columns:
            df_display[link_col] = df_display[link_col].apply(_fighter_profile_link)

    if "Date" in df_display.columns:
        df_display["Date"] = pd.to_datetime(df_display["Date"], errors="coerce").dt.strftime("%Y-%m-%d")

    st.markdown(df_display.to_html(index=False, escape=False), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Events History
# ---------------------------------------------------------------------------


def page_events_history() -> None:
    st.header(f"{t('page.events_history.title')}")

    if st.button("Clear all filters", key="events_history_clear_filters"):
        st.session_state["events_history_query"] = ""
        st.session_state["events_history_phase"] = "Upcoming"
        st.session_state["events_history_location"] = "All locations"
        st.session_state["events_history_selected_event"] = "All events (current filters)"
        st.session_state.pop("events_history_start_date", None)
        st.session_state.pop("events_history_end_date", None)
        st.rerun()

    df_upcoming = _read_parquet(FOLDER_UPCOMING, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    if df_upcoming.empty:
        df_upcoming = _read_parquet(FOLDER_UPCOMING_CATBOOST, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    df_events = _read_parquet(FOLDER_EVENTS, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    df_past = _read_parquet(FOLDER_HIST_ALL, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    if df_past.empty:
        df_past = _read_parquet(FOLDER_HIST_ALL_CATBOOST, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    def _clean_text_col(series: pd.Series) -> pd.Series:
        out = series.astype(str).str.strip()
        return out.where(~out.str.lower().isin({"", "nan", "nat", "none"}), other="")

    def _first_non_empty(series: pd.Series) -> str:
        for val in series:
            txt = "" if val is None else str(val).strip()
            if txt and txt.lower() not in {"nan", "nat", "none"}:
                return txt
        return ""

    def _event_meta_from_df(df_src: pd.DataFrame, phase_hint: str | None = None) -> pd.DataFrame:
        if df_src.empty or "event_name" not in df_src.columns:
            return pd.DataFrame(columns=["event_name", "event_date", "location", "event_phase"])

        work = pd.DataFrame()
        work["event_name"] = _clean_text_col(df_src["event_name"])
        work = work[work["event_name"] != ""]
        if work.empty:
            return pd.DataFrame(columns=["event_name", "event_date", "location", "event_phase"])

        if "event_date" in df_src.columns:
            work["event_date"] = pd.to_datetime(df_src["event_date"], errors="coerce")
        else:
            work["event_date"] = pd.NaT

        if "location" in df_src.columns:
            work["location"] = _clean_text_col(df_src["location"])
        else:
            work["location"] = ""

        event_meta = (
            work.groupby("event_name", as_index=False)
            .agg(
                event_date=("event_date", "max"),
                location=("location", _first_non_empty),
            )
        )
        if phase_hint in {"Upcoming", "Past"}:
            event_meta["event_phase"] = phase_hint
        else:
            today_date = pd.Timestamp.now().date()
            event_meta["event_phase"] = event_meta["event_date"].apply(
                lambda dt: "Upcoming" if pd.notna(dt) and pd.Timestamp(dt).date() >= today_date else "Past"
            )
        return event_meta

    event_frames = [
        _event_meta_from_df(df_events),
        _event_meta_from_df(df_upcoming, "Upcoming"),
        _event_meta_from_df(df_past, "Past"),
    ]
    event_frames = [frame for frame in event_frames if not frame.empty]
    if not event_frames:
        st.info(t("page.events_history.no_data"))
        return

    df_events_meta = pd.concat(event_frames, ignore_index=True)
    df_events_meta = (
        df_events_meta.sort_values("event_date", ascending=False, na_position="last")
        .groupby("event_name", as_index=False)
        .agg(
            event_date=("event_date", "max"),
            location=("location", _first_non_empty),
        )
    )
    today_date = pd.Timestamp.now().date()
    df_events_meta["event_phase"] = df_events_meta["event_date"].apply(
        lambda dt: "Upcoming" if pd.notna(dt) and pd.Timestamp(dt).date() >= today_date else "Past"
    )

    df_filtered_events = df_events_meta.copy()
    all_locations_label = "All locations"
    selected_location = all_locations_label

    with st.expander("Event Filters", expanded=True):
        top_left, top_right = st.columns(2)

        phase_options = ["All", "Upcoming", "Past"]
        phase_state = st.session_state.get("events_history_phase", "Upcoming")
        if phase_state not in phase_options:
            phase_state = "Upcoming"
        with top_left:
            phase = st.selectbox(
                "Event window",
                phase_options,
                index=phase_options.index(phase_state),
                key="events_history_phase",
            )
        if phase != "All" and "event_phase" in df_filtered_events.columns:
            df_filtered_events = df_filtered_events[df_filtered_events["event_phase"] == phase].copy()

        with top_right:
            if "location" in df_filtered_events.columns:
                location_clean = _clean_text_col(df_filtered_events["location"])
                location_options = sorted([loc for loc in location_clean.unique().tolist() if loc])
                if location_options:
                    location_values = [all_locations_label] + location_options
                    location_state = st.session_state.get("events_history_location", all_locations_label)
                    if location_state not in location_values:
                        location_state = all_locations_label
                    selected_location = st.selectbox(
                        "Location filter",
                        location_values,
                        index=location_values.index(location_state),
                        key="events_history_location",
                    )
                    if selected_location != all_locations_label:
                        df_filtered_events = df_filtered_events[location_clean == selected_location].copy()
                else:
                    st.caption("Location filter: not enough location data")

        if "event_date" in df_filtered_events.columns and df_filtered_events["event_date"].notna().any():
            date_series = pd.to_datetime(df_filtered_events["event_date"], errors="coerce")
            min_date = date_series.min()
            max_date = date_series.max()
            if pd.notna(min_date) and pd.notna(max_date):
                default_start = st.session_state.get("events_history_start_date", min_date.date())
                default_end = st.session_state.get("events_history_end_date", max_date.date())
                if default_start < min_date.date() or default_start > max_date.date():
                    default_start = min_date.date()
                if default_end < min_date.date() or default_end > max_date.date():
                    default_end = max_date.date()

                d1, d2 = st.columns(2)
                with d1:
                    start_date = st.date_input(
                        "From date",
                        value=default_start,
                        min_value=min_date.date(),
                        max_value=max_date.date(),
                        key="events_history_start_date",
                    )
                with d2:
                    end_date = st.date_input(
                        "To date",
                        value=default_end,
                        min_value=min_date.date(),
                        max_value=max_date.date(),
                        key="events_history_end_date",
                    )

                if start_date > end_date:
                    st.warning("From date is after To date.")
                    return

                mask = date_series.notna() & date_series.dt.date.between(start_date, end_date)
                df_filtered_events = df_filtered_events[mask].copy()

        if df_filtered_events.empty:
            st.info(t("page.events_history.no_data"))
            return

        events_sorted = (
            df_filtered_events[["event_name", "event_date"]]
            .drop_duplicates()
            .sort_values("event_date", ascending=False, na_position="last")
        )
        event_names = events_sorted["event_name"].dropna().astype(str).tolist()
        if not event_names:
            st.info(t("page.events_history.no_data"))
            return

        lower_left, lower_right = st.columns(2)
        with lower_left:
            event_query = st.text_input(
                "Search event",
                value=st.session_state.get("events_history_query", ""),
                placeholder="Type event name (e.g. UFC 300) ...",
                key="events_history_query",
            )
        suggested_events = _rank_text_options(event_names, event_query)
        if not suggested_events:
            st.info("No events match your search.")
            return

        all_events_label = "All events (current filters)"
        selectable_events = [all_events_label] + suggested_events
        selected_state = st.session_state.get("events_history_selected_event", all_events_label)
        if selected_state not in selectable_events:
            selected_state = all_events_label
        with lower_right:
            selected_event = st.selectbox(
                "Event selected",
                selectable_events,
                index=selectable_events.index(selected_state),
                key="events_history_selected_event",
            )

        st.caption(
            f"Active filters • Window: {phase} • Location: {selected_location} • Matches: {len(df_filtered_events):,} events"
        )
    if selected_event == all_events_label:
        selected_events_set = set(df_filtered_events["event_name"].astype(str).tolist())
        df_event_meta = df_filtered_events.copy()
    else:
        selected_events_set = {str(selected_event)}
        df_event_meta = df_filtered_events[df_filtered_events["event_name"].astype(str) == str(selected_event)].copy()

    fight_cols = [
        "event_name",
        "event_date",
        "location",
        "fighter_name_display",
        "opponent_name_display",
        "winner_name_display",
    ]

    def _fight_rows_for_event(df_src: pd.DataFrame, phase_label: str) -> pd.DataFrame:
        if df_src.empty or "event_name" not in df_src.columns:
            return pd.DataFrame()
        cols = [c for c in fight_cols if c in df_src.columns]
        if not cols:
            return pd.DataFrame()
        tmp = df_src[cols].copy()
        tmp["event_name"] = _clean_text_col(tmp["event_name"])
        tmp = tmp[tmp["event_name"].isin(selected_events_set)]
        if tmp.empty:
            return pd.DataFrame()
        if "event_date" in tmp.columns:
            tmp["event_date"] = pd.to_datetime(tmp["event_date"], errors="coerce")
        else:
            tmp["event_date"] = pd.NaT
        if "location" not in tmp.columns:
            tmp["location"] = ""
        else:
            tmp["location"] = _clean_text_col(tmp["location"])
        tmp["event_phase"] = phase_label
        return tmp

    fights_frames = [
        _fight_rows_for_event(df_upcoming, "Upcoming"),
        _fight_rows_for_event(df_past, "Past"),
        _fight_rows_for_event(df_events, "Upcoming"),
    ]
    fights_frames = [frame for frame in fights_frames if not frame.empty]
    if fights_frames:
        df_event_fights = pd.concat(fights_frames, ignore_index=True).drop_duplicates()
    else:
        df_event_fights = pd.DataFrame(columns=fight_cols + ["event_phase"])

    event_loc_fallback = ""
    if not df_event_meta.empty and "location" in df_event_meta.columns:
        event_loc_fallback = str(df_event_meta["location"].iloc[0]).strip()
    if event_loc_fallback:
        if "location" in df_event_fights.columns:
            df_event_fights["location"] = _clean_text_col(df_event_fights["location"])
            df_event_fights["location"] = df_event_fights["location"].where(df_event_fights["location"] != "", other=event_loc_fallback)

    fights_count = len(df_event_fights)
    fighters_left = set(df_event_fights.get("fighter_name_display", pd.Series(dtype=str)).dropna().astype(str))
    fighters_right = set(df_event_fights.get("opponent_name_display", pd.Series(dtype=str)).dropna().astype(str))
    unique_fighters = len(fighters_left | fighters_right)
    event_date = pd.to_datetime(df_event_meta.get("event_date"), errors="coerce").max()
    event_date_text = event_date.strftime("%Y-%m-%d") if pd.notna(event_date) else "—"

    event_location = "Multiple locations" if selected_event == all_events_label else "—"
    if selected_event != all_events_label and "location" in df_event_meta.columns:
        loc_series = (
            df_event_meta["location"]
            .dropna()
            .astype(str)
            .str.strip()
        )
        loc_series = loc_series[(loc_series != "") & (~loc_series.str.lower().isin({"nan", "nat", "none"}))]
        if not loc_series.empty:
            event_location = loc_series.iloc[0]

    _render_fighter_overview_card(
        [
            ("Event Date", event_date_text),
            ("Fights", str(int(fights_count))),
            ("Fighters", str(int(unique_fighters))),
            ("Location", event_location),
        ],
        title="Event Snapshot",
    )

    if st.checkbox("Show event location map", value=True, key="events_history_show_map"):
        _render_events_location_map(
            df_event_meta if not df_event_meta.empty else df_filtered_events,
            selected_event if selected_event != all_events_label else "",
            max_locations=(70 if selected_event != all_events_label else 36),
        )

    show_cols = [
        "event_date",
        "event_phase",
        "event_name",
        "location",
        "fighter_name_display",
        "opponent_name_display",
        "winner_name_display",
    ]
    show_cols = [c for c in show_cols if c in df_event_fights.columns]
    tbl = df_event_fights[show_cols].copy()

    tbl = tbl.rename(
        columns={
            "event_date": "Date",
            "event_phase": "Window",
            "event_name": "Event",
            "location": "Location",
            "fighter_name_display": "Fighter",
            "opponent_name_display": "Opponent",
            "winner_name_display": "Winner",
        }
    )

    if "Date" in tbl.columns:
        tbl["Date"] = pd.to_datetime(tbl["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        tbl = tbl.sort_values(["Date", "Event"], ascending=[False, True], na_position="last")

    for link_col in ["Fighter", "Opponent", "Winner"]:
        if link_col in tbl.columns:
            tbl[link_col] = tbl[link_col].apply(_fighter_profile_link)

    st.subheader("Fighters per event")
    if tbl.empty:
        st.info("No fight-level rows found for this event.")
    else:
        st.markdown(tbl.to_html(index=False, escape=False), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Belt Holders
# ---------------------------------------------------------------------------


def _belt_holders_champion_map(
    base: str, prefix: str = "",
) -> dict[str, dict]:
    """Load belt holders parquet and return {weight_class: info} map.

    Also used by the rankings page for accurate champion badges.
    """
    df_bh = _read_parquet(FOLDER_BELT_HOLDERS, base, prefix)
    if df_bh.empty:
        return {}
    result: dict[str, dict] = {}
    for _, row in df_bh.iterrows():
        wc = str(row.get("weight_class", "")).strip()
        if not wc:
            continue
        is_vacant = False
        vac_val = row.get("is_vacant")
        if isinstance(vac_val, bool):
            is_vacant = vac_val
        elif isinstance(vac_val, str):
            is_vacant = vac_val.strip().lower() in ("true", "1", "yes")
        result[wc] = {
            "champion_fighter_id": str(row.get("champion_fighter_id", "") or "").strip(),
            "champion_fighter_name": str(row.get("champion_fighter_name", "") or "").strip(),
            "title_won_date": row.get("title_won_date"),
            "title_won_event": str(row.get("title_won_event", "") or "").strip(),
            "title_defenses": int(row.get("title_defenses", 0) or 0),
            "last_title_fight_date": row.get("last_title_fight_date"),
            "is_vacant": is_vacant,
        }
    return result


def page_belt_holders() -> None:
    st.header(t("page.belt_holders.title"))

    df_belt = _read_parquet(FOLDER_BELT_HOLDERS, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    df_history = _read_parquet(FOLDER_TITLE_FIGHT_HISTORY, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    df_manual = _read_parquet(FOLDER_MANUAL_TITLE_VACATES, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    if df_belt.empty:
        st.info(t("page.belt_holders.no_data"))
        return

    # ---- Current Champions section ----
    st.subheader(t("page.belt_holders.current_champions"))

    # KPI cards
    total_divisions = len(df_belt)
    active_champs = 0
    if "is_vacant" in df_belt.columns:
        vac_mask = df_belt["is_vacant"].apply(
            lambda v: (
                (v is True)
                or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))
            )
            if v is not None
            else False
        )
        active_champs = int((~vac_mask).sum())
    else:
        active_champs = total_divisions

    total_title_fights = len(df_history) if not df_history.empty else 0
    title_changes = 0
    if not df_history.empty and "title_changed_hands" in df_history.columns:
        tc_mask = df_history["title_changed_hands"].apply(
            lambda v: (
                (v is True)
                or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))
            )
            if v is not None
            else False
        )
        title_changes = int(tc_mask.sum())

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _render_kpi_card(
            t("page.belt_holders.total_divisions"),
            str(total_divisions),
            icon=_goat_icon_html(),
            accent="#3b82f6",
        )
    with c2:
        _render_kpi_card(
            t("page.belt_holders.active_champions"),
            str(active_champs),
            icon=_goat_icon_html(),
            accent="#f59e0b",
        )
    with c3:
        _render_kpi_card(
            t("page.belt_holders.total_title_fights"),
            str(total_title_fights),
            icon=_goat_icon_html(),
            accent="#a855f7",
        )
    with c4:
        _render_kpi_card(
            t("page.belt_holders.title_changes"),
            str(title_changes),
            icon=_goat_icon_html(),
            accent="#ef4444",
        )

    st.divider()

    # Interactive current champions table
    champions_tbl = df_belt.copy()
    if "is_vacant" in champions_tbl.columns:
        champions_tbl["is_vacant"] = champions_tbl["is_vacant"].apply(
            lambda v: (
                (v is True)
                or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))
            )
            if v is not None
            else False
        )
    else:
        champions_tbl["is_vacant"] = False

    for dt_col in ["title_won_date", "last_title_fight_date"]:
        if dt_col in champions_tbl.columns:
            champions_tbl[dt_col] = pd.to_datetime(champions_tbl[dt_col], errors="coerce")
            champions_tbl[dt_col] = champions_tbl[dt_col].dt.strftime("%Y-%m-%d")

    if "champion_fighter_name" in champions_tbl.columns:
        champions_tbl["champion_fighter_name"] = champions_tbl.apply(
            lambda r: t("page.belt_holders.vacant")
            if bool(r.get("is_vacant", False)) or not str(r.get("champion_fighter_name", "") or "").strip()
            else str(r.get("champion_fighter_name", "") or "").strip(),
            axis=1,
        )

    if "champion_fighter_name" in champions_tbl.columns:
        champions_tbl["Flag"] = champions_tbl.apply(
            lambda r: (
                "—"
                if bool(r.get("is_vacant", False))
                else _fighter_badge(
                    r.get("champion_fighter_name"),
                    fighter_id=r.get("champion_fighter_id"),
                    country=_resolve_fighter_country(
                        r.get("champion_fighter_name"),
                        fighter_id=r.get("champion_fighter_id"),
                    ),
                    is_champion=True,
                )
            ),
            axis=1,
        )

    show_cols = [
        "Flag",
        "weight_class",
        "champion_fighter_name",
        "title_won_date",
        "title_won_event",
        "title_defenses",
        "last_title_fight_date",
        "is_vacant",
    ]
    show_cols = [c for c in show_cols if c in champions_tbl.columns]
    champions_tbl = champions_tbl[show_cols].copy()

    champions_tbl = champions_tbl.rename(
        columns={
            "Flag": "Ftr",
            "weight_class": "Division",
            "champion_fighter_name": t("page.belt_holders.champion"),
            "title_won_date": t("page.belt_holders.title_won"),
            "title_won_event": "Title Event",
            "title_defenses": t("page.belt_holders.defenses"),
            "last_title_fight_date": t("page.belt_holders.last_title_fight"),
            "is_vacant": "Vacant",
        }
    )

    if "Vacant" in champions_tbl.columns:
        champions_tbl["Vacant"] = champions_tbl["Vacant"].map({True: "Yes", False: "No"})

    _render_smart_dataframe(
        champions_tbl.sort_values("Division") if "Division" in champions_tbl.columns else champions_tbl,
        key="belt_holders_current",
        height=360,
    )

    # ---- Manual Belt Overrides section ----
    st.divider()
    st.subheader(t("page.belt_holders.manual_overrides"))
    if df_manual.empty:
        st.caption(t("page.belt_holders.manual_overrides_empty"))
    else:
        manual_cols = [
            "vacated_on",
            "weight_class",
            "champion_name",
            "reason",
            "notes",
        ]
        manual_cols = [c for c in manual_cols if c in df_manual.columns]
        manual_tbl = df_manual[manual_cols].copy()
        if "vacated_on" in manual_tbl.columns:
            manual_tbl["vacated_on"] = pd.to_datetime(manual_tbl["vacated_on"], errors="coerce")
            manual_tbl = manual_tbl.sort_values("vacated_on", ascending=False)
            manual_tbl["vacated_on"] = manual_tbl["vacated_on"].dt.strftime("%Y-%m-%d")
        manual_tbl = manual_tbl.rename(
            columns={
                "vacated_on": "Date",
                "weight_class": "Division",
                "champion_name": "Champion",
                "reason": "Reason",
                "notes": "Notes",
            }
        )
        _render_smart_dataframe(manual_tbl, key="belt_holders_manual", height=260)

    # ---- Title Fight History section ----
    if df_history.empty:
        return

    st.divider()
    st.subheader(t("page.belt_holders.title_fight_history"))

    # Division filter
    if "weight_class" in df_history.columns:
        divisions = sorted(df_history["weight_class"].dropna().unique().tolist())
        selected_div = st.selectbox(
            t("page.belt_holders.filter_division"),
            [t("page.belt_holders.all_divisions")] + divisions,
        )
        if selected_div != t("page.belt_holders.all_divisions"):
            df_hist_show = df_history[df_history["weight_class"] == selected_div].copy()
        else:
            df_hist_show = df_history.copy()
    else:
        df_hist_show = df_history.copy()

    # Sort most recent first
    if "event_date" in df_hist_show.columns:
        df_hist_show["event_date"] = pd.to_datetime(
            df_hist_show["event_date"], errors="coerce"
        )
        df_hist_show = df_hist_show.sort_values("event_date", ascending=False)

    # Display table
    show_cols = [
        "event_date",
        "weight_class",
        "winner_name",
        "loser_name",
        "method",
        "fight_round",
        "fight_time",
        "title_changed_hands",
        "was_vacant",
        "title_defense_number",
        "event_name",
    ]
    show_cols = [c for c in show_cols if c in df_hist_show.columns]
    tbl = df_hist_show[show_cols].copy()

    if "winner_name" in tbl.columns:
        tbl["W Ftr"] = tbl["winner_name"].apply(
            lambda n: _fighter_badge(
                n,
                country=_resolve_fighter_country(n),
                is_champion=True,
            ) if str(n or "").strip() else "—"
        )
    if "loser_name" in tbl.columns:
        tbl["L Ftr"] = tbl["loser_name"].apply(
            lambda n: _fighter_badge(
                n,
                country=_resolve_fighter_country(n),
            ) if str(n or "").strip() else "—"
        )

    # Format
    if "event_date" in tbl.columns:
        tbl["event_date"] = tbl["event_date"].dt.strftime("%Y-%m-%d")
    if "title_changed_hands" in tbl.columns:
        tbl["title_changed_hands"] = tbl["title_changed_hands"].apply(
            lambda v: "Yes" if v is True or (isinstance(v, str) and v.lower() in ("true", "1")) else "No"
        )
    if "was_vacant" in tbl.columns:
        tbl["was_vacant"] = tbl["was_vacant"].apply(
            lambda v: "Yes" if v is True or (isinstance(v, str) and v.lower() in ("true", "1")) else "No"
        )

    tbl = tbl.rename(
        columns={
            "W Ftr": "W Ftr",
            "L Ftr": "L Ftr",
            "event_date": "Date",
            "weight_class": "Division",
            "winner_name": "Winner",
            "loser_name": "Loser",
            "method": "Method",
            "fight_round": "Rd",
            "fight_time": "Time",
            "title_changed_hands": "Title Changed",
            "was_vacant": "Vacant",
            "title_defense_number": "Defense #",
            "event_name": "Event",
        }
    )

    front_cols = [c for c in ["W Ftr", "Winner", "L Ftr", "Loser"] if c in tbl.columns]
    other_cols = [c for c in tbl.columns if c not in front_cols]
    tbl = tbl[front_cols + other_cols]

    _render_smart_dataframe(tbl, key="belt_holders_history", height=420)


# ---------------------------------------------------------------------------
# Page: Fighter Rankings
# ---------------------------------------------------------------------------


def page_rankings() -> None:
    st.markdown(
        f"<h2 class='fp-section-title'>{_goat_icon_html(size=24)}<span>Fighter Rankings</span></h2>",
        unsafe_allow_html=True,
    )

    df_rank = _read_parquet(FOLDER_RANKINGS, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)

    # Use belt_holders data as the source of truth for current champions
    # (one champion per weight class, determined by title-fight lineage).
    belt_map = _belt_holders_champion_map(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    champion_ids: set[str] = set()
    champion_names: set[str] = set()
    champion_ids_per_wc: dict[str, str] = {}   # weight_class -> fighter_id
    champion_names_per_wc: dict[str, str] = {}  # weight_class -> fighter_name

    for wc, info in belt_map.items():
        if info.get("is_vacant"):
            continue
        fid = info.get("champion_fighter_id", "")
        fname = info.get("champion_fighter_name", "")
        if fid:
            champion_ids.add(fid)
            champion_ids_per_wc[wc] = fid
        if fname:
            champion_names.add(fname)
            champion_names_per_wc[wc] = fname

    # Fallback: if belt_holders data is not available, use lineage maps
    if not champion_ids and not champion_names:
        lineage = _title_lineage_maps(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
        champion_ids = set(lineage.get("current_ids", set()))
        champion_names = set(lineage.get("current_names", set()))

    if df_rank.empty:
        st.info("No ranking data available.")
        return

    # As-of date badge
    if "as_of_date" in df_rank.columns:
        as_of = pd.to_datetime(df_rank["as_of_date"], errors="coerce").max()
        st.caption(f"Rankings as of **{as_of:%Y-%m-%d}**")

    # Weight class filter
    classes = sorted(df_rank["weight_class"].dropna().unique().tolist())
    selected_class = st.selectbox("Weight class", ["All"] + classes)

    if selected_class != "All":
        df_show = df_rank[df_rank["weight_class"] == selected_class].copy()
    else:
        df_show = df_rank.copy()

    # Status filter (default: active only)
    status_view = st.selectbox(
        "Fighter status",
        ["Active", "Inactive", "All"],
        index=0,
    )
    if "fighter_status" in df_show.columns:
        status_norm = df_show["fighter_status"].astype(str).str.strip().str.lower()
        if status_view == "Active":
            df_show = df_show[status_norm == "active"]
        elif status_view == "Inactive":
            df_show = df_show[status_norm == "inactive"]

    # Top-N slider
    max_rank = int(df_show["rank"].max()) if "rank" in df_show.columns and not df_show.empty else 50
    top_n = st.slider("Show top N per class", min_value=5, max_value=min(max_rank, 100), value=15)
    df_show = df_show[df_show["rank"] <= top_n]

    # Quick stats
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _render_kpi_card("Ranked Entries", str(len(df_show)), icon=_goat_icon_html(), accent="#a855f7")
    with c2:
        _render_kpi_card("Weight Classes", str(df_show["weight_class"].nunique()), icon=_goat_icon_html(), accent="#3b82f6")
    if champion_ids and "fighter_id" in df_show.columns:
        champs = df_show["fighter_id"].astype(str).str.strip().isin(champion_ids).sum()
    else:
        champs = (df_show["rank"] == 1).sum()
    with c3:
        _render_kpi_card("Current Champs", str(int(champs)), icon=_goat_icon_html(), accent="#f59e0b")
    avg_points = pd.to_numeric(df_show.get("points"), errors="coerce").mean() if "points" in df_show.columns else pd.NA
    avg_points_text = f"{float(avg_points):.1f}" if pd.notna(avg_points) else "—"
    with c4:
        _render_kpi_card("Avg Points", avg_points_text, icon=_goat_icon_html(), accent="#ef4444")

    st.divider()

    # Render table per weight class
    for wc, wc_df in df_show.groupby("weight_class", sort=True):
        # Per-division champion (from belt_holders, one champ per weight class)
        wc_champ_id = champion_ids_per_wc.get(wc, "")
        wc_champ_name = champion_names_per_wc.get(wc, "")

        st.subheader(str(wc))

        display_cols = [
            "rank", "fighter_id", "fighter_name", "country", "points", "fights_count",
            "wins_count", "losses_count", "draws_count",
            "win_streak", "title_defenses_count", "fighter_status",
        ]
        display_cols = [c for c in display_cols if c in wc_df.columns]
        tbl = wc_df[display_cols].copy().sort_values("rank")

        if "fighter_name" in tbl.columns:
            tbl["Badge"] = tbl.apply(
                lambda r, _wc_cid=wc_champ_id, _wc_cname=wc_champ_name: _fighter_badge(
                    r.get("fighter_name"),
                    fighter_id=r.get("fighter_id"),
                    country=r.get("country"),
                    gender=_gender_from_weight_class(r.get("weight_class")),
                    is_champion=(
                        (bool(_wc_cid) and str(r.get("fighter_id", "")).strip() == _wc_cid)
                        or (bool(_wc_cname) and str(r.get("fighter_name", "")).strip() == _wc_cname)
                        if (_wc_cid or _wc_cname)
                        else (int(r.get("rank", 999)) == 1 if pd.notna(r.get("rank")) else False)
                    ),
                ),
                axis=1,
            )

        if "country" in tbl.columns:
            tbl = tbl.drop(columns=["country"])

        tbl = tbl.rename(columns={
            "rank": "#",
            "fighter_name": "Fighter",
            "points": "Points",
            "fights_count": "Fights",
            "wins_count": "W",
            "losses_count": "L",
            "draws_count": "D",
            "win_streak": "Win Streak",
            "title_defenses_count": "Title Def.",
            "fighter_status": "Status",
            "Badge": "Ftr",
        })

        if "fighter_id" in tbl.columns:
            tbl = tbl.drop(columns=["fighter_id"])

        if "Ftr" in tbl.columns:
            front_cols = ["Ftr"] + [c for c in tbl.columns if c != "Ftr"]
            tbl = tbl[front_cols]

        if "Fighter" in tbl.columns:
            tbl["Fighter"] = tbl["Fighter"].apply(
                lambda name: (
                    f'<a href="?page=fighter-card&fighter={quote_plus(str(name))}">{escape(str(name))}</a>'
                    if pd.notna(name)
                    else ""
                )
            )
        st.markdown(tbl.to_html(index=False, escape=False), unsafe_allow_html=True)


def page_fighter_profile() -> None:
    df_profiles = _read_parquet(FOLDER_FIGHTER_PROFILES, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    if df_profiles.empty:
        st.header("Fighter Cards")
        st.info("No fighter card data available.")
        return

    name_col = "fighter_name_display" if "fighter_name_display" in df_profiles.columns else "fighter_name"
    if name_col not in df_profiles.columns:
        st.header("Fighter Cards")
        st.info("Fighter card data is incomplete.")
        return

    profile_name_cols = [
        c for c in ["fighter_name_display", "fighter_name", "fighter_name_plain"]
        if c in df_profiles.columns
    ]
    fighter_options = sorted(
        [
            n
            for n in df_profiles[name_col].dropna().astype(str).str.strip().tolist()
            if n
        ],
        key=lambda n: n.casefold(),
    )
    fighter_options = list(dict.fromkeys(fighter_options))
    if not fighter_options:
        st.header("Fighter Cards")
        st.info("No fighters found in profile dataset.")
        return

    alias_map = _build_fighter_alias_map(df_profiles, name_col)

    preferred_qp = str(st.query_params.get("fighter", "")).strip()
    preferred_cookie = _cookie_get(_COOKIE_SELECTED_FIGHTER, "").strip()
    preferred = preferred_qp or st.session_state.get("selected_fighter_profile") or preferred_cookie

    if preferred and preferred not in fighter_options:
        qp_ranked = _rank_fighter_options(fighter_options, preferred, alias_map=alias_map, limit=20)
        if qp_ranked:
            preferred = qp_ranked[0]

    initial_selected = st.session_state.get("fighter_profile_picker")
    if initial_selected not in fighter_options:
        initial_selected = preferred if preferred in fighter_options else fighter_options[0]

    ranked_options = fighter_options

    default_index = 0
    if preferred in ranked_options:
        default_index = ranked_options.index(preferred)
    elif initial_selected in ranked_options:
        default_index = ranked_options.index(initial_selected)

    selected_fighter = st.selectbox(
        "Find fighter",
        ranked_options,
        index=default_index,
        key="fighter_profile_picker",
        help="Start typing to search by name. Matches support exact, partial, and typo-tolerant search.",
    )
    st.session_state["selected_fighter_profile"] = selected_fighter
    st.query_params["fighter"] = selected_fighter
    _cookie_set(_COOKIE_SELECTED_FIGHTER, selected_fighter)

    prof = pd.DataFrame()
    for col in profile_name_cols:
        prof = df_profiles[df_profiles[col].astype(str) == str(selected_fighter)].copy()
        if not prof.empty:
            break
    if prof.empty:
        st.header("Fighter Cards")
        st.info("No profile row found for this fighter.")
        return
    p = prof.iloc[0]

    win_streak = p.get("win_streak")
    _win_streak = str(int(win_streak)) if pd.notna(win_streak) else "—"
    loss_streak = p.get("loss_streak")
    _loss_streak = str(int(loss_streak)) if pd.notna(loss_streak) else "—"
    no_contests = p.get("no_contests")
    _no_contests = str(int(no_contests)) if pd.notna(no_contests) else "0"

    def _fmt_float_compact(value: object, decimals: int = 2) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "—"
        try:
            return f"{float(value):.{decimals}f}"
        except Exception:
            return "—"

    def _fmt_pct_compact(value: object) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "—"
        try:
            pct = float(value)
            if pct <= 1.0:
                pct *= 100.0
            return f"{pct:.0f}%"
        except Exception:
            return "—"

    current_belts_count_raw = p.get("current_belts_count", 0)
    try:
        current_belts_count = int(float(current_belts_count_raw)) if pd.notna(current_belts_count_raw) else 0
    except Exception:
        current_belts_count = 0

    is_current_champion = _to_boolish(p.get("is_current_champion")) or current_belts_count > 0
    current_belts_raw = str(p.get("current_belt_weight_classes", "") or "").strip()
    legacy_belt_raw = str(p.get("belt", "") or "").strip()
    status_txt = str(p.get("fighter_status", "") or "").strip().lower()

    lineage = _title_lineage_maps(ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    current_ids = set(lineage.get("current_ids", set()))
    current_names = set(lineage.get("current_names", set()))
    current_classes_by_id = lineage.get("current_classes_by_id", {})
    current_classes_by_name = lineage.get("current_classes_by_name", {})
    former_champ_ids = set(lineage.get("former_ids", set()))
    former_champ_names = set(lineage.get("former_names", set()))

    fighter_id_txt = str(p.get("fighter_id", "") or "").strip()
    fighter_name_txt = str(selected_fighter or "").strip()

    computed_current_classes: list[str] = []
    if fighter_id_txt and fighter_id_txt in current_classes_by_id:
        computed_current_classes = sorted(current_classes_by_id.get(fighter_id_txt, set()))
    elif fighter_name_txt and fighter_name_txt in current_classes_by_name:
        computed_current_classes = sorted(current_classes_by_name.get(fighter_name_txt, set()))

    computed_current_count = len(computed_current_classes)
    computed_is_current = (
        (bool(fighter_id_txt) and fighter_id_txt in current_ids)
        or (bool(fighter_name_txt) and fighter_name_txt in current_names)
        or computed_current_count > 0
    )

    if computed_current_count > 0:
        current_belts_count = computed_current_count

    if computed_current_classes:
        current_belts_raw = ", ".join(computed_current_classes)

    is_current_champion = computed_is_current or is_current_champion
    is_former_champion = (
        (bool(fighter_id_txt) and fighter_id_txt in former_champ_ids)
        or (bool(fighter_name_txt) and fighter_name_txt in former_champ_names)
    ) and not is_current_champion

    has_legacy_belt_signal = bool(legacy_belt_raw) or ("champ" in status_txt)
    is_belt_holder = is_current_champion or has_legacy_belt_signal or is_former_champion

    belt_label = current_belts_raw if is_current_champion and current_belts_raw else (legacy_belt_raw if legacy_belt_raw else ("Former Champion" if is_former_champion else "Belt Holder"))
    if current_belts_count > 0:
        belt_count = min(2, current_belts_count)
    else:
        belt_parts = [part.strip() for part in re.split(r"\s*,\s*|\s*/\s*|\s+and\s+", belt_label, flags=re.I) if part.strip()]
        belt_count = min(2, len(belt_parts)) if belt_parts else 1
    belt_icons = "".join(_goat_icon_html(size=14) for _ in range(max(1, belt_count)))

    fighter_weight_class = ""
    for candidate in [
        "weight_class",
        "division",
        "current_weight_class",
        "primary_weight_class",
        "ufc_weight_class",
    ]:
        raw_value = p.get(candidate)
        if raw_value is not None and not pd.isna(raw_value):
            value = str(raw_value).strip()
            if value:
                fighter_weight_class = value
                break
    if not fighter_weight_class and computed_current_classes:
        fighter_weight_class = computed_current_classes[0]
    if not fighter_weight_class and current_belts_raw:
        fighter_weight_class = re.split(r"\s*,\s*|\s*/\s*|\s+and\s+", current_belts_raw, maxsplit=1, flags=re.I)[0].strip()

    finish_rate_card_value = p.get("finish_rate_win_shrunk")
    if finish_rate_card_value is None or pd.isna(finish_rate_card_value):
        finish_rate_card_value = p.get("finish_rate")

    sub_rate_card_value = p.get("sub_rate_win_shrunk")
    if sub_rate_card_value is None or pd.isna(sub_rate_card_value):
        sub_rate_card_value = p.get("sub_rate")

    country_header = _canonical_country_name(p.get("country", ""))
    belt_holder_html = ""
    if is_belt_holder:
        belt_holder_html = (
            '<div style="display:inline-flex;align-items:center;gap:0.38rem;'
            'width:fit-content;max-width:100%;'
            'padding:0.24rem 0.52rem;border-radius:999px;'
            'border:1px solid rgba(113,113,122,0.42);background:rgba(24,24,27,0.74);'
            'box-shadow:inset 0 0 0 1px rgba(255,255,255,0.03);'
            'margin:0 0 0.48rem 0;">'
            f'<span style="display:inline-flex;align-items:center;gap:0.18rem;line-height:1;">{belt_icons}</span>'
            '<span style="font-size:0.72rem;color:#d4d4d8;text-transform:uppercase;letter-spacing:0.08em;font-weight:800;">Belt Holder</span>'
            f'<span style="font-size:0.84rem;font-weight:700;color:#f4f4f5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{escape(belt_label)}</span>'
            '</div>'
        )
    title_card, title_side = st.columns([1.6, 4.4])
    with title_card:
        _render_fighter_card_html(
            name=str(selected_fighter),
            country=country_header,
            weight_class=fighter_weight_class,
            is_champion=is_current_champion,
            fighter_status=str(p.get("fighter_status", "") or ""),
            finish_rate=finish_rate_card_value,
            sub_rate=sub_rate_card_value,
            win_streak=p.get("win_streak"),
            loss_streak=p.get("loss_streak"),
            wins=p.get("wins"),
            losses=p.get("losses"),
        )
    with title_side:
        if belt_holder_html:
            st.markdown(belt_holder_html, unsafe_allow_html=True)
        _r1, _r2, _r3 = st.columns(3)
        with _r1:
            _render_fighter_meta_card("Str Def", _fmt_pct_compact(p.get("str_def")), icon=_goat_icon_html(), accent="#22c55e")
        with _r2:
            _render_fighter_meta_card("TD Def", _fmt_pct_compact(p.get("td_def")), icon=_goat_icon_html(), accent="#eab308")
        with _r3:
            _render_fighter_meta_card("SApM", _fmt_float_compact(p.get("sapm"), 2), icon=_goat_icon_html(), accent="#ef4444")

    dob_val = p.get("dob")
    dob_text = "—"
    if pd.notna(dob_val):
        try:
            dob_text = pd.to_datetime(dob_val, errors="coerce").strftime("%Y-%m-%d")
        except Exception:
            dob_text = str(dob_val)

    _record = f"{int(p.get('wins', 0))}-{int(p.get('losses', 0))}-{int(p.get('draws', 0))}"
    _total_fights = f"{int(p.get('total_fights', 0))}"
    win_rate = p.get("win_rate")
    _win_rate = f"{float(win_rate):.1%}" if pd.notna(win_rate) else "—"
    finish_rate = p.get("finish_rate")
    _finish_rate = f"{float(finish_rate):.1%}" if pd.notna(finish_rate) else "—"
    _bonuses_won = str(int(p.get("bonuses_won_count", 0) or 0))
    _longest_win = str(int(p.get("longest_win_streak", 0) or 0))
    _longest_loss = str(int(p.get("longest_loss_streak", 0) or 0))

    with title_side:
        st.markdown("<div style='height: 0.35rem;'></div>", unsafe_allow_html=True)
        _render_fighter_overview_card(
            [
                ("Record", _record),
                ("Total Fights", _total_fights),
                ("UFC Win Rate", _win_rate),
                ("UFC Finish Rate", _finish_rate),
            ],
            title="Career Snapshot",
            emphasized=True,
        )

        st.markdown("<div style='height: 0.3rem;'></div>", unsafe_allow_html=True)
        s1, s2, s3 = st.columns(3)
        with s1:
            _render_fighter_meta_card("Bonuses Won", _bonuses_won, icon=_goat_icon_html(), accent="#f59e0b")
        with s2:
            _render_fighter_meta_card("Longest Win Streak", _longest_win, icon=_goat_icon_html(), accent="#22c55e")
        with s3:
            _render_fighter_meta_card("Longest Loss Streak", _longest_loss, icon=_goat_icon_html(), accent="#ef4444")

    st.markdown("<div style='height: 0.55rem;'></div>", unsafe_allow_html=True)

    def _fmt_pct_card(value: object) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "—"
        try:
            pct = float(value)
            if pct <= 1.0:
                pct *= 100.0
            return f"{pct:.1f}%"
        except Exception:
            return "—"

    bayes_metrics = [
        ("Bayes KO/TKO", _fmt_pct_card(p.get("ko_rate_win_shrunk"))),
        ("Bayes Sub", _fmt_pct_card(p.get("sub_rate_win_shrunk"))),
        ("Bayes Finish", _fmt_pct_card(p.get("finish_rate_win_shrunk"))),
        ("Method Sample", str(int(p.get("wins_method_known_count", 0) or 0))),
    ]
    st.markdown(
        "<div style='font-size:1.08rem;font-weight:700;letter-spacing:0.01em;margin:0 0 0.4rem 0;'>Profile Metrics View</div>",
        unsafe_allow_html=True,
    )

    metrics_view = st.radio(
        "Profile metrics view",
        ["Bayesian", "Striking", "Grappling"],
        horizontal=True,
        key="fighter_profile_metrics_view",
        label_visibility="collapsed",
    )

    def _fmt_float(value: object, decimals: int = 2) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "—"
        try:
            return f"{float(value):.{decimals}f}"
        except Exception:
            return "—"

    def _fmt_pct(value: object) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "—"
        try:
            pct = float(value)
            if pct <= 1.0:
                pct *= 100.0
            return f"{pct:.0f}%"
        except Exception:
            return "—"

    if metrics_view == "Bayesian":
        _render_fighter_overview_card(bayes_metrics, title="Bayesian Method Metrics")
        st.caption(
            "Bayes Finish uses Bayesian shrinkage on finish methods from pre-fight history. "
            "It blends each fighter's observed finish outcomes with a global UFC prior so small-sample fighters are less noisy. "
            "Method Sample is the historical fight count used for that estimate."
        )
    elif metrics_view == "Striking":
        _render_fighter_overview_card(
            [
                ("SLpM", _fmt_float(p.get("slpm"), 2)),
                ("SApM", _fmt_float(p.get("sapm"), 2)),
                ("Str Acc", _fmt_pct(p.get("str_acc"))),
                ("Str Def", _fmt_pct(p.get("str_def"))),
            ],
            title="Striking Metrics",
        )
    elif metrics_view == "Grappling":
        _render_fighter_overview_card(
            [
                ("TD Avg", _fmt_float(p.get("td_avg"), 2)),
                ("TD Acc", _fmt_pct(p.get("td_acc"))),
                ("TD Def", _fmt_pct(p.get("td_def"))),
                ("Sub Avg", _fmt_float(p.get("sub_avg"), 2)),
            ],
            title="Grappling Metrics",
        )

    st.markdown("<div style='height: 1cm;'></div>", unsafe_allow_html=True)

    country_raw = _canonical_country_name(p.get("country", ""))
    country_text = "—"
    country_html_labels: set[str] = set()
    if country_raw:
        if _country_flag_mode() == "cdn":
            country_img = _country_to_flagcdn_img(country_raw, width=20)
            if country_img:
                country_text = f"{country_img} {escape(country_raw)}"
                country_html_labels.add("Country")
            else:
                country_flag = _country_to_flag(country_raw)
                country_text = f"{country_raw} {country_flag}".strip()
        else:
            country_flag = _country_to_flag(country_raw)
            country_text = f"{country_raw} {country_flag}".strip()

    meta_specs = [
        ("DOB", dob_text),
        ("Country", country_text),
        ("Stance", str(p.get("stance", "—") or "—")),
        ("Weight", str(p.get("weight", "—") or "—")),
        ("Reach", str(p.get("reach", "—") or "—")),
        ("Height", str(p.get("height", "—") or "—")),
        ("Age", str(p.get("age", "—") or "—")),
        ("Status", str(p.get("fighter_status", "—") or "—")),
    ]
    _render_fighter_overview_card(meta_specs, html_value_labels=country_html_labels)

    st.divider()
    st.subheader("Full Fight History")

    df_hist_all = _read_parquet(FOLDER_FIGHTER_HISTORY, ACTIVE_PARQUET_BASE, ACTIVE_PREFIX)
    if df_hist_all.empty:
        st.info(
            "No fighter history data available. Run the dashboard parquet ETL with `--dataset fighter_history` (or `--dataset all`) "
            "and verify the sidebar Parquet prefix matches where exports were written."
        )
        return

    fighter_id = p.get("fighter_id")
    if pd.notna(fighter_id) and "fighter_id" in df_hist_all.columns:
        df_hist = df_hist_all[df_hist_all["fighter_id"].astype(str) == str(fighter_id)].copy()
    else:
        hist_name_col = "fighter_name_display" if "fighter_name_display" in df_hist_all.columns else name_col
        df_hist = df_hist_all[df_hist_all[hist_name_col].astype(str) == str(selected_fighter)].copy()

    if df_hist.empty:
        st.info("No fights found for this fighter.")
        return

    if "event_date" in df_hist.columns:
        df_hist["event_date"] = pd.to_datetime(df_hist["event_date"], errors="coerce")
        df_hist = df_hist.sort_values("event_date", ascending=False)

    show_cols = [
        "event_date",
        "event_name",
        "opponent_name_display",
        "weight_class",
        "result",
        "winner_name_display",
        "method",
        "round",
        "time",
        "is_title_fight",
        "kd_for",
        "str_for",
        "td_for",
        "sub_for",
        "kd_against",
        "str_against",
        "td_against",
        "sub_against",
    ]
    show_cols = [c for c in show_cols if c in df_hist.columns]
    df_show = df_hist[show_cols].rename(
        columns={
            "event_date": "Date",
            "event_name": "Event",
            "opponent_name_display": "Opponent",
            "weight_class": "Weight Class",
            "result": "Result",
            "winner_name_display": "Winner",
            "method": "Method",
            "round": "Rnd",
            "time": "Time",
            "is_title_fight": "Title Fight",
            "kd_for": "KD For",
            "str_for": "STR For",
            "td_for": "TD For",
            "sub_for": "SUB For",
            "kd_against": "KD Against",
            "str_against": "STR Against",
            "td_against": "TD Against",
            "sub_against": "SUB Against",
        }
    )

    if "Date" in df_show.columns:
        df_show["Date"] = pd.to_datetime(df_show["Date"], errors="coerce").dt.strftime("%Y-%m-%d")

    if "Result" in df_show.columns:
        def _result_badge(val: object) -> str:
            txt = "" if val is None else str(val).strip().lower()
            if txt in {"win", "w", "won"}:
                return '<span class="result-badge result-win">Win</span>'
            if txt in {"loss", "l", "lost"}:
                return '<span class="result-badge result-loss">Loss</span>'
            if txt in {"no contest", "nc", "draw", "d", "dq"}:
                return '<span class="result-badge result-nc">No Contest</span>'
            return escape("" if val is None else str(val))

        df_show["Result"] = df_show["Result"].apply(_result_badge)

    if "Opponent" in df_show.columns:
        df_show["Opponent"] = df_show["Opponent"].apply(
            lambda name: (
                f'<a href="?page=fighter-card&fighter={quote_plus(str(name))}">{escape(str(name))}</a>'
                if pd.notna(name) and str(name).strip()
                else ""
            )
        )

    st.markdown(df_show.to_html(index=False, escape=False), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

if _active_page_slug in ("home", "predictions", "upcoming"):
    page_upcoming()
elif _active_page_slug == "terms":
    page_terms()
elif _active_page_slug in ("fight-lab", "historical-picks", "model-performance"):
    page_historical()
elif _active_page_slug == "events-history":
    page_events_history()
elif _active_page_slug == "rankings":
    page_rankings()
elif _active_page_slug == "belt-holders":
    page_belt_holders()
elif _active_page_slug in {"fighter-card", "fighter-profile"}:
    page_fighter_profile()
