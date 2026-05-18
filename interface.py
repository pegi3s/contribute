import streamlit as st
import re
import urllib.request
import pandas as pd
import json
import ssl
import os
import subprocess
from datetime import datetime
from pathlib import Path
import time
import socket
import streamlit.components.v1 as components
import shutil
import zipfile
import requests
import io
import tempfile
import html
from streamlit_scroll_to_top import scroll_to_here


# base directory where all project-related data wiil be stored
BASE_PATH = "/data"
os.makedirs(BASE_PATH, exist_ok=True)

# diretory used to store project history
HISTORY_DIR = "/contribute_history"
os.makedirs(HISTORY_DIR, exist_ok=True)

# file JSON that keeps track of all projects and their states
DB_FILE = os.path.join(HISTORY_DIR, "projects_history.json")


# --- GENERAL FUNCTIONS ---
def load_local_db():
    """Loads the history from the JSON file if it exists."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            st.error(f"Error loading history: {e}")
    return {}


def save_local_db():
    """Saves the current state of projects_db to the disk."""
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(st.session_state.projects_db, f, indent=4)
    except Exception as e:
        st.error(f"Error saving data: {e}")


def prepare_submission_folder(project):
    """Prepare a clean 'for_submission' folder."""
    project_dir = Path(BASE_PATH) / project
    submission_dir = project_dir / "for_submission"

    submission_dir.mkdir(exist_ok=True)

    # -----------------------------
    # 1. MOVE FILES
    # -----------------------------
    exclude = {"for_submission", "test_data"}

    for item in project_dir.iterdir():
        if item.name in exclude:
            continue

        dst = submission_dir / item.name

        if item.is_file():
            shutil.move(str(item), str(dst))

    # -----------------------------
    # 2. HANDLE TEST DATA
    # -----------------------------
    ptype = st.session_state.get("project_type", None)
    
    # skip test packaging for external images without Dockerfile
    if ptype != "from_image_without_df":
        test_dir = project_dir / "test_data"
        submission_test_dir = submission_dir / "test_data"

        input_src = test_dir / "data"
        output_src = test_dir / "results"

        input_dst = submission_test_dir / "input_test_data"
        output_dst = submission_test_dir / "output_test_data"

        input_dst.mkdir(parents=True, exist_ok=True)
        output_dst.mkdir(parents=True, exist_ok=True)

        # INPUT FILES
        input_files = list(input_src.glob("*"))

        if len(input_files) == 1:
            # single file -> copy directly
            shutil.copy(input_files[0], input_dst / input_files[0].name)
        elif len(input_files) > 1:
            # multiple files -> zip them
            zip_path = input_dst / f"{project}.zip"
            with zipfile.ZipFile(zip_path, "w") as z:
                for f in input_files:
                    z.write(f, arcname=f.name)

        # OUTPUT -> always zipped
        output_zip = output_dst / f"{project}_output.zip"

        with zipfile.ZipFile(output_zip, "w") as z:
            for f in output_src.glob("*"):
                z.write(f, arcname=f.name)

    return submission_dir


# --- HELPER FUNCTION FOR ONTOLOGY ---
def get_ontology_path(term_id, terms_map, relations):
    """Builds the 'Parent > Child' path recursively for intuitive visualization."""
    if not term_id:
        return ""
    path = [terms_map.get(term_id, term_id)]
    current_id = term_id

    # 10-level limit to prevent infinite recursion in case of errors in the .obo file
    for _ in range(10):
        parent_id = relations.get(current_id)
        if not parent_id or parent_id not in terms_map:
            break
        path.insert(0, terms_map[parent_id])
        current_id = parent_id

    return " > ".join(path)


def apply_primary_button_style():
    """Apply custom styling to Streamlit primary buttons"""
    st.markdown("""
    <style>

    button[kind="primary"] {
        background-color: #059669 !important;
        color: white !important;
        border: none !important;
    }

    button[kind="primary"]:hover {
        background-color: #047857 !important;
    }

    button[kind="primary"]:active {
        background-color: #065f46 !important;
    }

    button[kind="primary"][disabled],
    button[kind="primary"]:disabled {
        background-color: #9ca3af !important;
        color: #e5e7eb !important;
        cursor: not-allowed !important;
        opacity: 0.6 !important;
        transform: none !important;
        box-shadow: none !important;
    }

    button[kind="primary"][disabled]:hover {
        background-color: #9ca3af !important;
    }

    </style>
    """, unsafe_allow_html=True)


# --- FUNCTION TO READ ONTOLOGY FROM GITHUB ---
@st.cache_data(ttl=3600)
def get_remote_dio_data():
    """
    Fetch ontology data from GitHub:
    - dio.obo -> terms + hierarchy
    - dio.diaf -> tool mappings
    """
    ontology = {}
    relations = {} 
    diaf_data = []
    context = ssl._create_unverified_context()

    # 1. Load dio.obo (ID -> Name + relations)
    try:
        obo_url = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/dio.obo"
        with urllib.request.urlopen(obo_url, context=context) as response:
            content = response.read().decode("utf-8")

            term_id = None

            for line in content.splitlines():
                line = line.strip()

                if line.startswith("id:"):
                    term_id = line.split("id:")[1].strip()

                elif line.startswith("name:") and term_id:
                    ontology[term_id] = line.split("name:")[1].strip()

                elif line.startswith("is_a:") and term_id:
                    parent_id = line.split("is_a:")[1].split()[0].strip()
                    relations[term_id] = parent_id

                elif line == "":
                    term_id = None

    except Exception as e:
        print(f"Error loading dio.obo: {e}")

    # 2. Load dio.diaf
    try:
        diaf_url = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/dio.diaf"
        with urllib.request.urlopen(diaf_url, context=context) as response:
            content = response.read().decode("utf-8")
            for line in content.splitlines():
                if "\t" in line:
                    parts = line.split("\t")
                    diaf_data.append({"id": parts[0].strip(), "tool": parts[1].strip()})
    except Exception as e:
        print(f"Error loading dio.diaf: {e}")

    return ontology, relations, diaf_data


# --- REMOTE FILE FETCHING ---
@st.cache_data(ttl=3600)
def fetch_dockerfile(project, version):
    """Fetch Dockerfile from the repository."""
    BASE_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master"

    paths = [
        (f"{BASE_URL}/{project}/{version}/Dockerfile", True, False),
        (f"{BASE_URL}/{project}/{version}/dockerfile", True, True),
        (f"{BASE_URL}/{project}/Dockerfile", False, False),
        (f"{BASE_URL}/{project}/dockerfile", False, True)
    ]

    for url, use_version, use_lower in paths:
        try:
            res = requests.get(url)
            if res.status_code == 200:
                return res.text, url, use_version, use_lower
        except:
            pass

    return "", None, None, None


@st.cache_data(ttl=3600)
def fetch_readme(project, version):
    """Fetch README.md from the repository."""
    BASE_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master"

    paths = [
        (f"{BASE_URL}/{project}/{version}/README.md", True),
        (f"{BASE_URL}/{project}/README.md", False)
    ]

    for url, use_version in paths:
        try:
            res = requests.get(url)
            if res.status_code == 200:
                return res.text, url, use_version
        except:
            pass

    return "", None, None


@st.cache_data(ttl=3600)
def get_remote_metadata():
    """Fetch global metadata.json from the repository."""
    try:
        url = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/metadata.json"
        context = ssl._create_unverified_context()

        with urllib.request.urlopen(url, context=context) as response:
            data = json.loads(response.read().decode())
            return data

    except Exception as e:
        print(f"Error fetching metadata: {e}")
        return []


def get_project_metadata(project_name):
    """Retrieve metadata for a specific project."""
    data = get_remote_metadata()

    if isinstance(data, list):
        for item in data:
            if item.get("name") == project_name:
                return item

    elif isinstance(data, dict):
        return data.get(project_name)

    return {}


def build_docker_image(project, project_dir):

    build_cmd = [
        "docker",
        "build",
        "-t",
        project.lower(),
        str(project_dir),
    ]

    progress_bar = st.progress(0)
    status_text = st.empty()

    log_lines = []

    process = subprocess.Popen(
        build_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True
    )

    progress = 0

    for line in iter(process.stdout.readline, ""):

        if line.strip():

            log_lines.append(line)

            progress = min(progress + 1, 95)
            progress_bar.progress(progress)

            status_text.text(line.strip())

    process.stdout.close()
    process.wait()

    progress_bar.progress(100)

    full_log = "".join(log_lines)

    with st.expander("📄 Build Log"):
        st.code(full_log)

    return process.returncode == 0, log_lines


# --- MAPPING BETWEEN README AND METADATA ---
METADATA_TO_README = {
    "tool_name": "tool_name",        # special (lower + no spaces)
    "tool_url": "source_url",
    "tool_url_help": "manual_url",
    "source_url": "source_url",
    "manual_url": "manual_url",
}

README_TO_METADATA = {
    "tool_name": "tool_name",        # special (format back)
    "source_url": "tool_url",
    "manual_url": "tool_url_help",
    "source_url": "source_url",
    "manual_url": "manual_url",
}

def normalize_toolname(value):
    """Convert tool name to metadata format (lowercase, no spaces)"""
    if not value:
        return value
    return value.replace(" ", "").lower()


def prettify_toolname(value):
    """Convert tool name to display format (README)"""
    if not value:
        return value
    return value.capitalize()


def prefill_if_empty(target, source, mapping, direction):
    """Fill target fields with values from source if they are empty."""
    for target_field, source_field in mapping.items():

        current_val = target.get(target_field)

        # skip if already has a real value
        if current_val is not None and str(current_val).strip() != "":
            continue

        val = source.get(source_field)

        if not val or not str(val).strip():
            continue

        # special case: tool name formatting
        if target_field == "tool_name":
            if direction == "readme_to_metadata":
                val = normalize_toolname(val)
            elif direction == "metadata_to_readme":
                val = prettify_toolname(val)

        target[target_field] = val


# --- DIRTY STATE MANAGEMENT ---
DIRTY_SECTIONS = ["dockerfile", "readme", "metadata", "ontology", "test_instructions"]

def dirty_key(section):
    """Generate session_state key for dirty tracking."""
    return f"{section}_dirty"


def mark_dirty(section, current, saved):
    """Mark a section as dirty if current value differs from saved value."""
    key = dirty_key(section)
    is_dirty = current != saved
    st.session_state[key] = is_dirty
    return is_dirty


def set_section_clean(section):
    """Mark a section as clean (no unsaved changes)."""
    st.session_state[dirty_key(section)] = False


def is_section_dirty(section):
    """Check if a section has unsaved changes."""
    return st.session_state.get(dirty_key(section), False)


# --- NAVIGATION CONTROL ---
def navigation_guard(target_page):
    """Prevent navigation if there are unsaved changes."""
    dirty_sections = [
        s for s in DIRTY_SECTIONS
        if is_section_dirty(s)
    ]

    if dirty_sections:
        show_unsaved_dialog(target_page)
        st.stop()

    change_page(target_page)
    st.rerun()


def autosave_dirty_sections():
    """Automatically persist any modified sections before navigation."""
    project = st.session_state.get("active_project")
    if not project or project not in st.session_state.projects_db:
        return

    p_data = st.session_state.projects_db[project]
    saved_any = False

    # --- DOCKERFILE ---
    dockerfile_key = f"dockerfile_{project}"
    if dockerfile_key in st.session_state:
        dockerfile_value = st.session_state[dockerfile_key]
    elif st.session_state.get("current_page") == "Dockerfile":
        dockerfile_value = st.session_state.get("dockerfile_temp", p_data.get("dockerfile", ""))
    else:
        dockerfile_value = p_data.get("dockerfile", "")

    if is_section_dirty("dockerfile") or dockerfile_value != p_data.get("dockerfile", ""):
        p_data["dockerfile"] = dockerfile_value
        set_section_clean("dockerfile")
        saved_any = True

    # --- README ---
    readme_manual_key = f"readme_manual_{project}"
    readme_key = f"readme_{project}"
    if readme_manual_key in st.session_state:
        readme_value = st.session_state[readme_manual_key]
    elif readme_key in st.session_state:
        readme_value = st.session_state[readme_key]
    else:
        readme_value = p_data.get("readme_raw", p_data.get("readme", ""))

    if is_section_dirty("readme") or readme_value != p_data.get("readme", ""):
        p_data["readme"] = readme_value
        p_data["readme_raw"] = readme_value
        set_section_clean("readme")
        saved_any = True

    # --- METADATA ---
    raw_json = st.session_state.get(
        f"metadata_json_raw_{project}",
        p_data.get("metadata_json_raw", "")
    )

    if is_section_dirty("metadata") or raw_json != p_data.get("metadata_json_raw", ""):
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            p_data["metadata_invalid_raw"] = raw_json
            st.session_state[f"metadata_json_raw_{project}"] = raw_json
            save_local_db()
            st.error("Cannot autosave Metadata because the JSON is invalid. Please fix it before using the sidebar.")
            st.stop()

        p_data["metadata_json"] = parsed
        p_data["metadata_preview_json"] = parsed
        p_data["metadata_json_raw"] = raw_json
        p_data.pop("metadata_invalid_raw", None)
        set_section_clean("metadata")
        saved_any = True

    # --- ONTOLOGY ---
    if is_section_dirty("ontology"):
        selected_terms = [
            key.replace("ontology_", "", 1)
            for key, value in st.session_state.items()
            if key.startswith("ontology_") and value is True
        ]
        p_data["ontology_terms"] = selected_terms
        set_section_clean("ontology")
        saved_any = True

    # --- TEST INSTRUCTIONS ---
    test_instructions = st.session_state.get(
        f"test_instructions_{project}",
        p_data.get("test_instructions", "")
    )

    if is_section_dirty("test_instructions") or test_instructions != p_data.get("test_instructions", ""):
        p_data["test_instructions"] = test_instructions
        set_section_clean("test_instructions")
        saved_any = True

    if saved_any:
        save_local_db()
        st.toast("Changes autosaved.")


def sidebar_navigation(target_page):
    """Navigate via sidebar with autosave."""
    autosave_dirty_sections()
    change_page(target_page)
    st.rerun()


def discard_unsaved_section_changes():
    """Discard all unsaved changes and restore last saved state."""
    project = st.session_state.get("active_project")
    if not project or project not in st.session_state.projects_db:
        for section in DIRTY_SECTIONS:
            set_section_clean(section)
        return

    p_data = st.session_state.projects_db[project]

    # --- DOCKERFILE ---
    if is_section_dirty("dockerfile"):
        for key in [f"dockerfile_{project}", "dockerfile_temp"]:
            if key in st.session_state:
                del st.session_state[key]

    # --- README ---
    if is_section_dirty("readme"):
        saved_readme = p_data.get("readme", "")
        p_data["readme_raw"] = saved_readme
        p_data["readme_source"] = "saved"

        for key in [f"readme_{project}", f"readme_manual_{project}"]:
            if key in st.session_state:
                del st.session_state[key]

    # --- METADATA ---
    if is_section_dirty("metadata"):
        saved_metadata = p_data.get("metadata_json", {})
        saved_metadata_raw = json.dumps(saved_metadata, indent=4, ensure_ascii=False) if saved_metadata else ""

        p_data["metadata_preview_json"] = saved_metadata
        p_data["metadata_json_raw"] = saved_metadata_raw
        p_data["metadata_source"] = "saved"

        metadata_raw_key = f"metadata_json_raw_{project}"
        if metadata_raw_key in st.session_state:
            del st.session_state[metadata_raw_key]

    # --- ONTOLOGY ---
    if is_section_dirty("ontology"):
        for key in list(st.session_state.keys()):
            if key.startswith("ontology_"):
                del st.session_state[key]

    # --- TEST INSTRUCTIONS ---
    if is_section_dirty("test_instructions"):
        key = f"test_instructions_{project}"
        if key in st.session_state:
            del st.session_state[key]

    # Reset all dirty flags
    for section in DIRTY_SECTIONS:
        set_section_clean(section)


@st.dialog("⚠️ Unsaved Changes")
def show_unsaved_dialog(target_page):
    """Confirmation dialog shown when user tries to leave with unsaved changes."""
    st.warning("You have unsaved changes. Are you sure you want to leave this page?")

    col1, col2 = st.columns(2)

    if col1.button("✅ Yes, leave without saving", use_container_width=True):
        discard_unsaved_section_changes()
        
        st.session_state["show_dialog"] = False
        
        change_page(target_page)
        st.rerun()

    if col2.button("❌ Cancel", use_container_width=True):
        st.session_state["show_dialog"] = False
        st.rerun()

def nav_button(label, target_page, **kwargs):
    """Navigation button with guard against unsaved changes."""
    if st.button(label, **kwargs):
        navigation_guard(target_page)
        
def nav_button_sidebar(label, target_page, **kwargs):
    """Navigation button with guard against unsaved changes."""

    if st.sidebar.button(label, **kwargs):
        navigation_guard(target_page)


# 1. PAGE CONFIGURATION
st.set_page_config(page_title="pegi3s BDIP", layout="wide")


@st.cache_data(ttl=3600)
def get_remote_built_list():
    """
    Fetch list of already built tools from remote metadata.
    Returns a sorted list of tool names.
    """
    try:
        url = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/metadata.json"
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(url, context=context) as response:
            data = json.loads(response.read().decode())
            if isinstance(data, list):
                return sorted([str(item.get("name", "Unknown")) for item in data])
            elif isinstance(data, dict):
                return sorted(list(data.keys()))
            return []
    except Exception as e:
        print(f"Detailed error: {e}")
        return []


def change_page(name):
    """Update current page in session state."""
    st.session_state.current_page = name
    scroll()


@st.dialog("⚠️ Reset All Data")
def confirm_reset_dialog():
    """
    Confirm dialog to delete all projects and local data.
    Requires explicit user confirmation ("DELETE").
    """
    st.warning("This will **permanently delete ALL projects**.")
    st.caption("This action cannot be undone.")

    confirm = st.text_input(
        "Type DELETE to confirm",
        key="reset_confirm"
    )

    col1, col2 = st.columns(2)

    # Cancel action
    if col1.button("Cancel", use_container_width=True):
        st.rerun()

    # Delete action (only enabled if confirmed)
    delete_disabled = confirm != "DELETE"

    if col2.button(
        "Delete All",
        use_container_width=True,
        disabled=delete_disabled,
        type="primary"
    ):
        st.session_state.projects_db = {}
        st.session_state.active_project = None

        # remove local database file
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)

        change_page("Home")
        st.rerun()


def sidebar_nave():
    
    """Sidebar navigation component."""
    page_options = [
        "Create Project",
        "Current Project",
        "Dockerfile",
        "README",
        "Metadata",
        "Ontology",
        "Build and Test",
        "Status",
    ]

    # Button Back to Home
    nav_button_sidebar("← Back to Home", "Home")

    current_index = (
        page_options.index(st.session_state.current_page)
        if st.session_state.current_page in page_options
        else 0
    )

    selected_page = st.sidebar.radio("Navigate to:", page_options, index=current_index)

    if selected_page != st.session_state.current_page:
        sidebar_navigation(selected_page)

    st.sidebar.divider()

    # reset button
    if st.sidebar.button("🗑️ Reset All Data", use_container_width=True):
        confirm_reset_dialog()


def get_progress(status_str):
    """Extract progress ratio -> "X/Y"."""
    try:
        if "/" in status_str:
            done, total = status_str.split(" ")[0].split("/")
            return int(done) / int(total)
    except:
        pass
    return 0.0


def get_type_color(ptype):
    """
    Return color associated with project type. 
    Used for UI badges and styling.
    """
    if ptype == "from_image_df":
        return "#5ba3d3"  #blue
    elif ptype == "from_image_without_df":
        return "#8669d6"  #purple
    elif ptype == "update":
        return "#e6962f"  #orange
    else:
        return "#28BD66"  #green (regular)


def format_type_label(ptype):
    """Convert internal project type to user-friendly label."""
    if ptype == "from_image_df":
        return "From Image (Dockerfile)"
    elif ptype == "from_image_without_df":
        return "From Image (No Dockerfile)"
    elif ptype == "update":
        return "Update"
    else:
        return "Regular"


def get_current_project_type():
    """Retrieve current project type."""
    project = st.session_state.get("active_project")

    if not project:
        return st.session_state.get("project_type", "regular")

    return st.session_state.projects_db.get(project, {}).get("project_type", "regular")


# --- NAV HANDLER ---
if "nav" not in st.session_state:
    st.session_state.nav = None

nav_event = components.html(
    """
<script>
window.addEventListener("message", (event) => {
    if (event.data && event.data.page) {
        const url = new URL(window.location);
        url.searchParams.set("page", event.data.page);
        window.location.href = url.toString();
    }
});
</script>
""",
    height=0,
)

# --- QUERY PARAM NAVIGATION ---
query_params = st.query_params

if "page" in query_params:
    st.session_state.current_page = query_params["page"]

if "current_page" not in st.session_state:
    st.session_state.current_page = "Home"

if "projects_db" not in st.session_state:
    st.session_state.projects_db = load_local_db()

if "active_project" not in st.session_state:
    st.session_state.active_project = None

if "test_success" not in st.session_state:
    st.session_state.test_success = False

st.session_state.built_list = get_remote_built_list()

if 'scroll_to_top' not in st.session_state:
    st.session_state.scroll_to_top = False

if st.session_state.scroll_to_top:
    scroll_to_here(0, key='top')

    st.session_state.scroll_to_top = False

def scroll():
    st.session_state.scroll_to_top = True

# --- HOME PAGE ---
if st.session_state.current_page == "Home":
    
    # CSS styling
    st.markdown("""
    <style>
        [data-testid="stSidebar"] {display: none;}

        .block-container {
            max-width: 1100px;
            margin: auto;
            padding-top: 2rem;
        }

        .hero-title {
            font-size: 52px;
            font-weight: 700;
            text-align: center;
            letter-spacing: -1px;
        }

        .hero-sub {
            font-size: 22px;
            text-align: center;
            margin-bottom: 30px;
            opacity: 0.8;
        }

        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(25px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .card-link {
            text-decoration: none !important;
            color: inherit !important;
            display: block;
            -webkit-tap-highlight-color: transparent;
        }

        .card-link:hover,
        .card-link:visited,
        .card-link:active {
            text-decoration: none !important;
            color: inherit !important;
        }
        
        .card-title {
            color: inherit;
        }

        .card-sub {
            color: inherit;
            opacity: 0.7;
        }

        /* Card-like buttons */
        div.stButton > button {
            height: 200px;
            width: 100%;
            border-radius: 16px;

            border: 1px solid rgba(59,130,246,0.15);
            background: linear-gradient(
                145deg,
                rgba(59,130,246,0.04),
                rgba(255,255,255,0.02)
            );

            backdrop-filter: blur(8px);
            padding: 25px;
            text-align: center;
            color: inherit;

            transition: all 0.25s ease;

            display: flex;
            flex-direction: column;
            justify-content: center;

            opacity: 0;
            animation: fadeInUp 0.5s ease forwards;
        }

        /* Button text styling */
        div.stButton > button p {
            margin: 0;
        }

        div.stButton > button p:nth-of-type(1) {
            font-size: 28px;
        }

        div.stButton > button p:nth-of-type(2) {
            font-size: 20px;
            font-weight: 600;
            margin-top: 10px;
            color: inherit;
        }

        div.stButton > button p:nth-of-type(3) {
            font-size: 13px;
            opacity: 0.7;
            margin-top: 6px;
        }

        /* Hover effect */
        div.stButton > button:hover {
            border: 1px solid #3b82f6;
            box-shadow: 0 10px 30px rgba(59,130,246,0.25);
            transform: translateY(-6px) scale(1.02);
            background: linear-gradient(
                145deg,
                rgba(59,130,246,0.10),
                rgba(59,130,246,0.05)
            );
        }

        /* Click effect */
        div.stButton > button:active {
            transform: scale(0.96);
        }

        /* Staggered animation */
        div.stButton:nth-of-type(1) > button { animation-delay: 0.2s; }
        div.stButton:nth-of-type(2) > button { animation-delay: 0.35s; }
        div.stButton:nth-of-type(3) > button { animation-delay: 0.5s; }
    </style>
    """, unsafe_allow_html=True)

    # --- Title ---
    st.markdown('<div class="hero-title">pegi3s BDIP</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hero-sub">Contribution Platform</div>', unsafe_allow_html=True
    )

    st.caption(
        "Select a workflow to begin your contribution or validation process:"
    )
    
    # --- Main actions ---
    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "🚀\n\nStart New Project\n\nCreate a new Docker image submission with full workflow.",
            use_container_width=True,
        ):
            st.session_state.current_page = "Create Project"
            st.rerun()

    with col2:
        if st.button(
            "⚙️\n\nTest Docker Image\n\nRun and validate an existing Docker image.",
            use_container_width=True,
        ):
            st.session_state.current_page = "Test Docker Image"
            st.rerun()

    # --- Command to download test input data ---
    BASE_CMD = """
    mkdir -p ./input_test_data && \
    cd ./input_test_data && \
    wget http://evolution6.i3s.up.pt/static/pegi3s/dockerfiles/input_test_data/index.txt && \
    awk '{print $1}' index.txt | tr -d '\\r' | xargs -I {} wget http://evolution6.i3s.up.pt/static/pegi3s/dockerfiles/input_test_data/{}
    """

    # --- Centered button ---
    col_center = st.columns([1, 2, 1])[1]

    with col_center:
        if st.button(
            "📦\n\nDownload Test Input Data\n\nDownload a complete set of sample input files to test your Docker image. Files will be stored in `/data/input_test_data`",
            use_container_width=True
        ):
            try:
                target_dir = Path("/data")
                target_dir.mkdir(parents=True, exist_ok=True)

                with st.spinner("Downloading and preparing test input data..."):
                    process = subprocess.Popen(
                        f"stdbuf -oL {BASE_CMD}",
                        shell=True,
                        cwd=target_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )

                    status_text = st.empty()

                    # Stream command output in real time
                    for line in iter(process.stdout.readline, ""):
                        clean_line = line.strip()
                        if clean_line:
                            status_text.text(clean_line)

                    process.stdout.close()
                    process.wait()

                if process.returncode == 0:
                    st.success("✅ Test input dataset successfully prepared!")
                    st.caption("Files available at: /data/input_test_data")

                else:
                    st.error("❌ Failed to download test input data")
                    
                    with st.expander("📄 Error details"):
                        st.code(process.stderr)

            except Exception as e:
                st.error(f"❌ Unexpected error: {e}")

    st.stop()


# Button Back to Home
#nav_button("← Back to Home", "Home")

st.markdown("""
<style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 1rem;
    }
</style>
""", unsafe_allow_html=True)

# --- CREATE PROJECT ---
if st.session_state.current_page == "Create Project":
    
    # inicialize project type and sidebar
    ptype = st.session_state.get("project_type", "regular")
    sidebar_nave()

    # project type options
    TYPE_OPTIONS = {
        "Regular": "regular",
        "From Image (Dockerfile)": "from_image_df",
        "From Image (No Dockerfile)": "from_image_without_df",
        "Update": "update",
    }

    if "project_type" not in st.session_state:
        st.session_state.project_type = "regular"

    reverse_map = {v: k for k, v in TYPE_OPTIONS.items()}

    # project type selector
    col1, col2 = st.columns([8, 1], vertical_alignment="bottom")
    
    with col1:
        selected_label = st.selectbox(
            "Project Type",
            options=list(TYPE_OPTIONS.keys()),
            index=list(TYPE_OPTIONS.values()).index(st.session_state.project_type),
            key="ptype_selector"
        )

    st.session_state.project_type = TYPE_OPTIONS[selected_label]

    color = get_type_color(st.session_state.project_type)

    # CSS styling -> selected type badge
    st.markdown(
        f"""
    <span style="
        background:{color};
        color:white;
        padding:6px 14px;
        border-radius:999px;
        font-size:13px;
        font-weight:600;
    ">
    {selected_label}
    </span>
    """,
        unsafe_allow_html=True,
    )
    
    ptype = TYPE_OPTIONS[selected_label]

    # info popover for each project type
    with col2:
        with st.popover("ℹ️"):
            if ptype == "regular":
                st.markdown("**Regular Project**")
                st.write(
                    "The most commonly used project type for pegi3s Docker images. "
                    "Build your tool from scratch using a Dockerfile, giving you full control over dependencies, environment, and execution."
                )
                st.link_button("🔗 View example", "https://github.com/pegi3s/dockerfiles/tree/master/clustalomega/1.2.4")

            elif ptype == "from_image_df":
                st.markdown("**From Image (with Dockerfile)**")
                st.write(
                    "Start from an existing Docker image and extend it with your own Dockerfile. "
                    "Use this when you want to reuse a base image but still customize or add functionality."
                )
                st.link_button("🔗 View example", "https://github.com/pegi3s/dockerfiles/tree/master/cd-hit")

            elif ptype == "from_image_without_df":
                st.markdown("**From Image (without Dockerfile)**")
                st.write(
                    "Use an existing Docker image directly, without creating a Dockerfile. "
                )
                st.link_button("🔗 View example", "https://github.com/pegi3s/dockerfiles/tree/master/auto-pss-genome")

            elif ptype == "update":
                st.markdown("**Update Project**")
                st.write(
                    "Create a new version of an existing project. "
                    "Use this to update tool versions, improve functionality, or apply fixes "
                    "while keeping the original structure."
                )
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # --- UPDATE ---
    if st.session_state.project_type == "update":
        st.markdown("## Select Image")
        st.caption("Search and choose an existing image")

        # -------------------------------------------------
        # SEARCH
        # -------------------------------------------------
        search = st.text_input("🔍 Search image", placeholder="e.g. samtools")

        built_projects = st.session_state.built_list

        if search:
            filtered = [p for p in built_projects if search.lower() in p.lower()]
        else:
            filtered = built_projects

        st.caption(f"{len(filtered)} images found")

        # -------------------------------------------------
        # PAGINATION CONFIG
        # -------------------------------------------------
        ITEMS_PER_PAGE = 12

        total_items = len(filtered)
        total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

        # page status
        if "page" not in st.session_state:
            st.session_state.page = 1

        # reset page if search change
        if "last_search" not in st.session_state:
            st.session_state.last_search = ""

        if search != st.session_state.last_search:
            st.session_state.page = 1
            st.session_state.last_search = search

        # limits
        st.session_state.page = max(1, min(st.session_state.page, total_pages))

        # slice
        start = (st.session_state.page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = filtered[start:end]

        # -------------------------------------------------
        # GRID
        # -------------------------------------------------
        cols = st.columns(4)

        selected = st.session_state.get("base_project")

        for i, proj in enumerate(page_items):
            with cols[i % 4]:
                label = proj
                if proj == selected:
                    label = f"✅ {proj}"

                if st.button(
                    label,
                    key=f"img_{proj}_{i}_{st.session_state.page}",
                    width="stretch",
                ):
                    st.session_state.base_project = proj
                    st.rerun()

        # -------------------------------------------------
        # PAGINATION CONTROLS 
        # -------------------------------------------------
        col_left, col_center, col_right = st.columns([3, 2, 3])

        with col_center:
            c1, c2, c3 = st.columns([1, 3, 1])

            # back
            with c1:
                if st.button("←", disabled=st.session_state.page == 1):
                    st.session_state.page -= 1
                    st.rerun()

            # actual page
            with c2:
                st.markdown(
                    f"<div style='text-align:center;'>Page {st.session_state.page} / {total_pages}</div>",
                    unsafe_allow_html=True,
                )

            # next
            with c3:
                if st.button("→", disabled=st.session_state.page == total_pages):
                    st.session_state.page += 1
                    st.rerun()

        # -------------------------------------------------
        # FEEDBACK
        # -------------------------------------------------
        if selected:
            st.success(f"Selected image: {selected}")

        # --- Fetch metadata for selected tool ---
        @st.cache_data(ttl=3600)
        def get_metadata_entry(tool):

            url = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/metadata.json"

            try:
                res = requests.get(url)
                data = res.json()

                if isinstance(data, list):
                    tool = tool.lower().strip()

                    for item in data:
                        name = item.get("name", "").lower().strip()

                        if name == tool:
                            return item

            except Exception as e:
                st.error(f"Metadata error: {e}")

            return {}

        # -------------------------------------------------
        # METADATA CARD
        # -------------------------------------------------
        if selected:
            meta = get_metadata_entry(selected)

            name = meta.get("name", selected)
            desc = meta.get("description", "No description available")

            # recommended
            recommended_list = meta.get("recommended", [])
            recommended = (
                recommended_list[0].get("version") if recommended_list else "N/A"
            )

            # latest
            latest = meta.get("latest", "N/A")

            st.markdown("<br>", unsafe_allow_html=True)

            col_info, col_actions = st.columns([1, 1])

            with col_info:
                st.markdown(
                    f"""
                <div style="
                    padding:16px;
                    border-radius:16px;
                    border:1px solid rgba(59,130,246,0.5);
                    background: rgba(255,255,255,0.03);
                    box-shadow: 0 8px 25px rgba(59,130,246,0.15);
                    max-width: 500px;
                ">
                    <h3>{name}</h3>
                    <p style="opacity:0.7;"><i>{desc}</i></p>
                    <b>Versions</b><br><br>
                    <span style="
                        background:#28BB4D;
                        color:white;
                        padding:6px 12px;
                        border-radius:999px;
                        font-size:13px;
                        font-weight:500;
                        margin-right:10px;
                    ">
                    Recommended: {recommended}
                    </span>
                    <span style="
                        background:#5ba3d3;
                        color:white;
                        padding:6px 12px;
                        border-radius:999px;
                        font-size:13px;
                        font-weight:500;
                    ">
                    Latest: {latest}
                    </span>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            with col_actions:
                col_info, col_actions = st.columns([1, 1])

                new_version = st.text_input(
                    "New version", placeholder="Enter new version (e.g. 1.0.0)"
                )

                st.markdown("<br>", unsafe_allow_html=True)

                if st.button("🚀 Create new version", width="stretch"):
                    if not new_version.strip():
                        st.error("Enter a version name")
                        st.stop()

                    version_clean = new_version.strip()

                    # -------------------------------------------------
                    # EXISTING VERSIONS
                    # -------------------------------------------------
                    existing_versions = set()

                    # latest
                    latest_meta = meta.get("latest")
                    if latest_meta:
                        existing_versions.add(str(latest_meta).strip())

                    # recommended
                    for item in meta.get("recommended", []):
                        v = item.get("version")
                        if v:
                            existing_versions.add(str(v).strip())

                    # no_longer_tested
                    for item in meta.get("no_longer_tested", []):
                        v = item.get("version")
                        if v:
                            existing_versions.add(str(v).strip())

                    #st.write(existing_versions)

                    # -------------------------------------------------
                    # VERSION ALREADY EXISTS
                    # -------------------------------------------------
                    if version_clean in existing_versions:
                        st.error(
                            f"Version '{version_clean}' already exists for '{selected}'"
                        )
                        st.stop()

                    project_name = f"{selected}-{new_version}"

                    if project_name in st.session_state.projects_db:
                        st.warning("Project already exists")
                        st.stop()

                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

                    st.session_state.projects_db[project_name] = {
                        "project_type": "update",
                        "mode": "update",
                        "based_on": selected,
                        "base_version": latest,
                        "dockerfile": "",
                        "readme": "",
                        "ontology_terms": [],
                        "status": "In Progress",
                        "date_created": now_str,
                    }

                    save_local_db()

                    st.success(f"✅ Created: {project_name}")

                    st.session_state.active_project = project_name
                    st.session_state.project_type = "update"

                    change_page("Current Project")
                    st.rerun()

        st.divider()

        new_version_projects = [
            (p, d)
            for p, d in st.session_state.projects_db.items()
            if d.get("mode") == "update"
        ]

        if new_version_projects:
            st.markdown("### Version Upgrades in Progress")

            n_cols = 3

            for i in range(0, len(new_version_projects), n_cols):
                cols = st.columns(n_cols)

                for j in range(n_cols):
                    if i + j >= len(new_version_projects):
                        continue

                    project, data = new_version_projects[i + j]

                    base = data.get("based_on", "unknown")
                    version = data.get("base_version", "unknown")

                    color = get_type_color("update")

                    st.markdown(
                        """
                    <style>

                    .project-card {
                        border-radius: 16px;
                        padding: 20px;
                        border: 1px solid rgba(128,128,128,0.2);
                        background: rgba(255,255,255,0.03);
                        margin-bottom: 15px;
                        transition: 0.2s;
                    }

                    .project-card:hover {
                        transform: translateY(-3px);
                        box-shadow: 0 6px 20px rgba(0,0,0,0.15);
                    }

                    .project-title {
                        font-size: 20px;
                        font-weight: 600;
                    }

                    .project-type {
                        font-size: 14px;
                        margin-top: 5px;
                    }

                    </style>
                    """,
                        unsafe_allow_html=True,
                    )

                    with cols[j]:
                        st.markdown(
                            f"""
                        <div class="project-card" style="
                            padding:16px;
                            border-radius:14px;
                            border: 1px solid rgba(128,128,128,0.2);
                            border-left: 5px solid {color};
                            background: linear-gradient(to right, {color}22, transparent);
                            margin-bottom:10px;
                        ">
                            <b>{project}</b><br>
                            <span style="opacity:0.7;">
                                Based on: {base}
                            </span><br>
                            <span style="opacity:0.7;">
                                Version: {version}
                            </span>
                        </div>
                        """,
                            unsafe_allow_html=True,
                        )

                        colA, colB = st.columns(2)

                        if colA.button(
                            "📂 Open", key=f"open_nv_{project}", width="stretch"
                        ):
                            st.session_state.active_project = project
                            change_page("Current Project")
                            st.rerun()

                        if colB.button(
                            "🗑️ Delete", key=f"del_nv_{project}", width="stretch"
                        ):
                            del st.session_state.projects_db[project]
                            save_local_db()
                            st.rerun()

                        st.markdown("</div>", unsafe_allow_html=True)

        st.stop()


    # 1. Start New Project Section
    st.markdown("### Start New Project")
    new_name = st.text_input("New Project Name:", placeholder="e.g. samtools-v1")

    if st.button("➕ Create New Submission", use_container_width=True):
        clean_name = new_name.strip()
        if clean_name == "":
            st.error("Please enter a name.")
        elif (
            clean_name in st.session_state.projects_db
            or clean_name in st.session_state.built_list
        ):
            st.warning("This project already exists!")
        else:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            ptype = st.session_state.get("project_type", "regular")

            st.session_state.projects_db[clean_name] = {
                "project_type": ptype,
                "dockerfile": "",
                "readme": "",
                "ontology_terms": [],
                "status": "In Progress",
                "date_created": now_str,
            }

            save_local_db()
            st.session_state.active_project = clean_name
            change_page("Current Project")
            st.rerun()

    st.divider()

    st.markdown(
        """
    <style>

    .project-card {
        border-radius: 16px;
        padding: 20px;
        border: 1px solid rgba(128,128,128,0.2);
        background: rgba(255,255,255,0.03);
        margin-bottom: 15px;
        transition: 0.2s;
    }

    .project-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 6px 20px rgba(0,0,0,0.15);
    }

    .project-title {
        font-size: 20px;
        font-weight: 600;
    }

    .project-type {
        font-size: 14px;
        margin-top: 5px;
    }

    </style>
    """,
        unsafe_allow_html=True,
    )

    # 2. Project list section
    st.markdown("### 🛠️ Images in Construction")

    if st.session_state.projects_db:
        col1, col2 = st.columns([1, 1])

        # VIEW MODE
        with col1:
            if "view_mode" not in st.session_state:
                st.session_state.view_mode = "Dashboard"

            view_mode = st.radio(
                "View mode",
                ["Dashboard", "Table"],
                horizontal=True,
                index=0 if st.session_state.view_mode == "Dashboard" else 1,
            )

            st.session_state.view_mode = view_mode

        # SEARCH / FILTER
        with col2:
            search = st.text_input(
                "🔍 Search project", placeholder="Type project name..."
            )

            projects = [
                (p, d)
                for p, d in st.session_state.projects_db.items()
                if d.get("mode", "standard") != "update"
            ]

            if search:
                projects = [(p, d) for p, d in projects if search.lower() in p.lower()]

            if not projects:
                st.info("No project found.")
                st.stop()

        # DASHBOARD VIEW
        def render_dashboard_view(projects):

            n_cols = 3

            for i in range(0, len(projects), n_cols):
                cols = st.columns(n_cols)

                for j in range(n_cols):
                    if i + j >= len(projects):
                        continue

                    project, data = projects[i + j]

                    ptype = data.get("project_type", "regular")
                    status = data.get("status", "In Progress")
                    progress = get_progress(status)
                    color = get_type_color(ptype)
                    label = format_type_label(ptype)

                    with cols[j]:
                        st.markdown(
                            f"""
                        <div class="project-card" style="
                            border-left: 5px solid {color};
                            background: linear-gradient(to right, {color}22, transparent);
                        ">
                            <div class="project-title">{project}</div>
                            <div class="project-type" style="color:{color};">{label}</div>
                        """,
                            unsafe_allow_html=True,
                        )

                        st.progress(progress)
                        st.caption(f"Status: {status}")

                        bcol1, bcol2 = st.columns(2)

                        if bcol1.button(
                            "📂 Open", key=f"open_{project}", use_container_width=True
                        ):
                            st.session_state.active_project = project
                            st.session_state.project_type = data.get(
                                "project_type", "regular"
                            )
                            change_page("Current Project")
                            st.rerun()

                        if bcol2.button(
                            "🗑️ Delete", key=f"del_{project}", use_container_width=True
                        ):
                            del st.session_state.projects_db[project]
                            save_local_db()

                            if st.session_state.active_project == project:
                                st.session_state.active_project = None

                            st.rerun()

                        st.markdown("</div>", unsafe_allow_html=True)

        # TABLE VIEW
        def render_table_view(projects):

            table_data = []

            for project, data in projects:
                table_data.append(
                    {
                        "Project": project,
                        "Type": format_type_label(data.get("project_type")),
                        "Status": data.get("status"),
                    }
                )

            df = pd.DataFrame(table_data)

            selection = st.dataframe(
                df,
                use_container_width=True,
                on_select="rerun",
                selection_mode="single-row",
            )

            if selection.selection.rows:
                idx = selection.selection.rows[0]
                selected_project = df.iloc[idx]["Project"]

                col1, col2 = st.columns(2)

                if col1.button("📂 Open", use_container_width=True):
                    st.session_state.active_project = selected_project
                    change_page("Current Project")
                    st.rerun()

                if col2.button("🗑️ Delete", use_container_width=True):
                    del st.session_state.projects_db[selected_project]
                    save_local_db()

                    if st.session_state.active_project == selected_project:
                        st.session_state.active_project = None

                    st.rerun()

        # RENDER VIEW
        if view_mode == "Dashboard":
            render_dashboard_view(projects)
        else:
            render_table_view(projects)

    else:
        st.info("No projects started. Use the form above.")

    st.divider()


# --- CURRENT PROJECT ---
elif st.session_state.current_page == "Current Project":
    sidebar_nave()

    project = st.session_state.active_project

    # validation: ensure a project is selected
    if not project:
        st.warning("Select a project first!")
        st.stop()
    st.header(f"Project: {project}")

    ptype = get_current_project_type()
    st.toast(f"Opened {project} ({format_type_label(ptype)})")

    # navigation buttons
    c1, c2, c3 = st.columns(3)
    if c1.button("📄 Dockerfile", use_container_width=True):
        navigation_guard("Dockerfile")
    if c2.button("📝 README", use_container_width=True):
        navigation_guard("README")
    if c3.button("📦 Metadata (JSON)", use_container_width=True):
        navigation_guard("Metadata")

    c4, c5, c6 = st.columns(3)
    if c4.button("🧬 Ontology", use_container_width=True):
        navigation_guard("Ontology")
    if c5.button("⚙️ Build and Test", use_container_width=True):
        navigation_guard("Build and Test")
    if c6.button("🚦 Project STATUS", use_container_width=True):
        navigation_guard("Status")
    st.divider()
    
    # status section
    st.header(f"Status")

    p_data = st.session_state.projects_db[project]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ----------------------------
    # PROJECT TYPE BADGE
    # ----------------------------
    ptype = p_data.get("project_type", "regular")
    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
        <span style="
            background:{color};
            color:white;
            padding:5px 12px;
            border-radius:999px;
            font-size:12px;
            font-weight:600;
        ">
        {label}
        </span>
        """,
        unsafe_allow_html=True,
    )

    # ----------------------------
    # AUTO STATUS FLAGS
    # ----------------------------
    auto_status = {
        "Dockerfile": "DONE ✅"
        if p_data.get("dockerfile") and len(p_data["dockerfile"]) > 10
        else "NOT DONE ❌",

        "README": "DONE ✅"
        if p_data.get("readme") and len(p_data["readme"]) > 10
        else "NOT DONE ❌",

        "Ontology": "DONE ✅"
        if p_data.get("ontology_terms")
        else "NOT DONE ❌",

        "Metadata": "DONE ✅"
        if p_data.get("meta_responses")
        else "NOT DONE ❌",

        "Build": "DONE ✅"
        if p_data.get("build_success")
        else "NOT DONE ❌",

        "Test": "DONE ✅"
        if p_data.get("test_success")
        else "NOT DONE ❌",
    }

    # adapt status based on project type
    if ptype == "from_image_without_df":
        auto_status.pop("Dockerfile", None)
        auto_status.pop("Build", None)

    if ptype == "update":
        auto_status.pop("Metadata", None)
        auto_status.pop("Ontology", None)

    # ----------------------------
    # INIT / SYNC MANUAL STATUS
    # ----------------------------
    manual = p_data.get("manual_status", [])

    if not manual:
        manual = [
            {"Step": step, "Status": stat, "Date": now_str}
            for step, stat in auto_status.items()
        ]
    else:
        # keep only valid steps
        manual = [row for row in manual if row["Step"] in auto_status]

        existing = {row["Step"] for row in manual}

        # add missing steps
        for step, stat in auto_status.items():
            if step not in existing:
                manual.append({"Step": step, "Status": stat, "Date": now_str})

        # auto upgrade status
        for row in manual:
            step = row["Step"]
            auto = auto_status.get(step)

            if auto == "DONE ✅" and row["Status"] != "DONE ✅":
                row["Status"] = "IN PROGRESS ⏳"
                row["Date"] = now_str

    # persist synchronized status
    st.session_state.projects_db[project]["manual_status"] = manual

    # ----------------------------
    # STATUS TABLE EDITOR
    # ----------------------------
    status_df = pd.DataFrame(manual)

    edited_df = st.data_editor(
        status_df,
        column_config={
            "Status": st.column_config.SelectboxColumn(
                "Status",
                options=["DONE ✅", "NOT DONE ❌", "IN PROGRESS ⏳", "REVIEW 🔍"],
                required=True,
            ),
            "Date": st.column_config.TextColumn(
                "Last Modification", disabled=True
            ),
        },
        disabled=["Step"],
        use_container_width=True,
        hide_index=True,
    )

    # ----------------------------
    # PROGRESS
    # ----------------------------
    done_count = (edited_df["Status"] == "DONE ✅").sum()
    total_count = len(edited_df)

    progress_percent = int((done_count / total_count) * 100) if total_count else 0

    st.session_state.projects_db[project]["status"] = (
        f"{done_count}/{total_count} Done"
    )

    st.progress(progress_percent)
    st.caption(f"{done_count}/{total_count} steps completed")

    # ----------------------------
    # BUTTONS STYLE
    # ----------------------------
    st.markdown(
        """
        <style>
        button[kind="primary"] {
            background-color: #059669 !important;
            color: white !important;
            border: none !important;
        }
        button[kind="primary"]:hover {
            background-color: #047857 !important;
        }
        button[kind="primary"]:active {
            background-color: #065f46 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)

    # ----------------------------
    # SAVE
    # ----------------------------
    if col1.button(
        "💾 Confirm Status Changes",
        type="primary",
        use_container_width=True,
    ):
        st.session_state.projects_db[project]["manual_status"] = edited_df.to_dict(
            "records"
        )

        save_local_db()
        st.success("Status updated and saved!")

    # ----------------------------
    # SUBMISSION LOGIC
    # ----------------------------
    all_done = total_count > 0 and done_count == total_count

    if all_done:
        st.success("All steps completed! Ready for submission.")
    else:
        st.info("Complete all steps to enable submission package generation.")

    # generate submission package
    if col2.button(
        "📦 Generate Submission Package",
        disabled=not all_done,
        use_container_width=True,
    ):
        try:
            submission_dir = prepare_submission_folder(project)
            st.success(f"Created at {submission_dir}")
        except Exception as e:
            st.error(f"Error: {e}")

    st.divider()


# --- DOCKERFILE PAGE ---
elif st.session_state.current_page == "Dockerfile":
    sidebar_nave()

    project = st.session_state.active_project
    
    # validation: ensure a project is selected
    if not project:
        st.warning("Select a project first!")
        st.stop()

    ptype = get_current_project_type()

    st.header(f"Dockerfile Editor - {project}")

    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
    <span style="
        background:{color};
        color:white;
        padding:5px 12px;
        border-radius:999px;
        font-size:12px;
        font-weight:600;
    ">
    {label}
    </span>
    """,
        unsafe_allow_html=True,
    )

    # --- UPDATE ---
    if ptype == "update":
        project = st.session_state.active_project
        data = st.session_state.projects_db.get(project, {})

        base = data.get("based_on")
        version = data.get("base_version")

        dockerfile_content, source_url, use_version, use_lower = fetch_dockerfile(base, version)

        key = f"dockerfile_{project}"

        # load existing Dockerfile
        if dockerfile_content:
            if source_url:
                st.caption(f"Loaded from: {source_url}")

            if not use_version:
                st.warning("⚠️ This project does not follow the expected version structure (missing version folder).")

            if use_lower:
                st.warning("⚠️ The Dockerfile is named 'dockerfile' (lowercase). It should be 'Dockerfile'.")

            if key not in st.session_state:
                saved = data.get("dockerfile")
                st.session_state[key] = saved if saved else dockerfile_content

            edited = st.text_area("Edit your Dockerfile", height=400, key=key)

        else:
            st.warning("No Dockerfile found in repository.")

            if key not in st.session_state:
                st.session_state[key] = ""

            edited = st.text_area("Dockerfile", height=400, key=key)

        st.warning("""
        ⚠️ Make sure all required files are present in the project folder.
        Docker can only access files inside this directory during build.
        """)

        col_save, col_check, col_build = st.columns(3)

        apply_primary_button_style()

        # ---------------------------------------
        # DIRTY STATE TRACKING (UNSAVED CHANGES)
        # ---------------------------------------
        saved_content = st.session_state.projects_db[project].get("dockerfile", "")
        is_dirty = mark_dirty("dockerfile", edited, saved_content)
        st.session_state["dockerfile_dirty"] = is_dirty

        if col_save.button(
            "💾 SAVE PROGRESS",
            use_container_width=True,
            type="primary",
            disabled=not is_dirty
        ):
            st.session_state.projects_db[project]["dockerfile"] = edited
            save_local_db()
            mark_dirty("dockerfile", edited, edited)
            set_section_clean("dockerfile")
            st.success("Dockerfile progress saved!")

        if col_check.button("✅ CHECK Dockerfile", width="stretch"):
            st.divider()
            st.subheader("Dockerfile Validation")

            validation_log = []
            issues = []

            def add_log(level, msg):
                timestamp = datetime.now().strftime("%H:%M:%S")
                validation_log.append(f"[{timestamp}] [{level.upper()}] {msg}")
                issues.append((level, msg))

            if not edited.strip():
                add_log("error", "Dockerfile is empty")

            if not re.search(r"^FROM\s+", edited, re.MULTILINE):
                add_log("error", "Missing FROM instruction")

            if issues:
                st.error("❌ Dockerfile validation failed")

                with st.expander("📄 Validation Log"):
                    st.code("\n".join(validation_log))

                st.stop()

            # hadolint integration
            def load_hadolint_ignores():
                RULES_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/hadolint.rules"
                try:
                    context = ssl._create_unverified_context()
                    with urllib.request.urlopen(RULES_URL, context=context) as response:
                        content = response.read().decode("utf-8")

                    rules = []
                    for line in content.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("-"):
                            line = line[1:].strip()
                        rules.extend([r.strip() for r in line.split(",") if r.strip()])
                    return rules
                except:
                    return []


            def parse_hadolint_output(output):
                errors = []
                warnings = []
                info = []
                syntax_errors = []

                pattern = r"(.+?):(\d+):(\d+)\s+(DL\d+)\s+(error|warning|info):\s+(.*)"

                for line in output.splitlines():
                    line_clean = line.strip()

                    match = re.match(pattern, line_clean, re.IGNORECASE)
                    if match:
                        severity = match.group(5).lower()

                        if severity == "error":
                            errors.append(line_clean)
                        elif severity == "warning":
                            warnings.append(line_clean)
                        elif severity == "info":
                            info.append(line_clean)
                    else:
                        if "unexpected" in line_clean.lower():
                            syntax_errors.append(line_clean)
                        else:
                            if "error" in line_clean.lower():
                                errors.append(line_clean)
                            elif "warning" in line_clean.lower():
                                warnings.append(line_clean)
                            elif "info" in line_clean.lower():
                                info.append(line_clean)

                return errors, warnings, info, syntax_errors

            # run hadolint if Docker available
            if shutil.which("docker"):
                ignore_rules = load_hadolint_ignores()

                cmd = ["docker", "run", "--rm", "-i", "hadolint/hadolint", "hadolint"]
                for rule in ignore_rules:
                    cmd.extend(["--ignore", rule])
                cmd.append("-")

                try:
                    with st.spinner("🔍 Running Hadolint..."):
                        progress = st.progress(0)

                        for i in range(30):
                            time.sleep(0.01)
                            progress.progress(i + 1)

                        result = subprocess.run(
                            cmd, input=edited, text=True, capture_output=True
                        )

                        for i in range(30, 100):
                            time.sleep(0.005)
                            progress.progress(i + 1)

                    progress.empty()

                    output = (result.stdout or "") + "\n" + (result.stderr or "")
                    output = output.strip()

                    errors_list, warnings_list, info_list, syntax_errors = (
                        parse_hadolint_output(output)
                    )

                    for e in errors_list:
                        add_log("error", e)

                    for w in warnings_list:
                        add_log("warning", w)

                    for i in info_list:
                        add_log("info", i)

                    for s in syntax_errors:
                        add_log("error", f"Syntax: {s}")

                    # result feedback
                    if any(level == "error" for level, _ in issues):
                        st.error("❌ Validation failed")
                    elif any(level == "warning" for level, _ in issues):
                        st.warning("⚠️ Validation passed with warnings")
                    else:
                        st.success("✅ No issues found!")

                    # report
                    full_log = "\n".join(validation_log)

                    if len(full_log) != 0:
                        with st.expander("📄 Full Validation Log"):
                            st.code(full_log)

                    # SAVE ONLY IF NO ERRORS
                    if not any(level == "error" for level, _ in issues):
                        project_dir = Path(BASE_PATH) / project
                        project_dir.mkdir(parents=True, exist_ok=True)
                        dockerfile_path = project_dir / "Dockerfile"

                        st.session_state.projects_db[project]["dockerfile"] = edited

                        with open(dockerfile_path, "w", encoding="utf-8") as f:
                            f.write(edited)

                        if "manual_status" in st.session_state.projects_db[project]:
                            current_status_list = st.session_state.projects_db[project][
                                "manual_status"
                            ]

                            for row in current_status_list:
                                if row["Step"] == "Dockerfile":
                                    row["Status"] = "DONE ✅"
                                    row["Date"] = datetime.now().strftime(
                                        "%Y-%m-%d %H:%M"
                                    )

                            done_count = sum(
                                1
                                for row in current_status_list
                                if row["Status"] == "DONE ✅"
                            )
                            st.session_state.projects_db[project]["status"] = (
                                f"{done_count}/{len(current_status_list)} Done"
                            )

                        save_local_db()
                        
                        mark_dirty("dockerfile", edited, edited)
                        set_section_clean("dockerfile")
                        
                        st.info(
                            f"📁 Dockerfile done and saved successfully to project folder: {dockerfile_path}!"
                        )

                except Exception as e:
                    st.error(f"Error running hadolint: {e}")

            else:
                st.info("💡 Docker not available. Cannot run hadolint.")

        project_dir = Path(BASE_PATH) / project

        # --- Build Docker Image ---
        if col_build.button("🚀 Build Docker Image", use_container_width=True):
            dockerfile_path = project_dir / "Dockerfile"

            if not dockerfile_path.exists():
                st.error("Dockerfile not found")
                st.stop()

            st.info("Starting Docker build...")
            
            success, log_lines = build_docker_image(project, project_dir)

            if success:
                st.success("✅ Docker image built successfully!")

                # UPDATE FLAGS
                st.session_state.projects_db[project]["build_success"] = True

                # UPDATE STATUS
                if "manual_status" in st.session_state.projects_db[project]:
                    for row in st.session_state.projects_db[project]["manual_status"]:
                        if row["Step"] == "Build":
                            row["Status"] = "DONE ✅"
                            row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

                save_local_db()

            else:
                st.error("❌ Build failed")
                st.code("\n".join(log_lines[-20:]))

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Current Project", "Current Project")

        with col3:
            nav_button("Continue to README →", "README")

        st.stop()

    # --- LICENSE WARNING ---
    st.warning(
        "⚠️ **Important – License Check Required**\n\n"
        "Before creating a Docker image, verify if the tool and its dependencies "
        "allow redistribution. Some bioinformatics tools have licenses that "
        "restrict containerization or public distribution.\n\n"
        "Please check the tool's **license**, **source repository**, or official "
        "documentation to confirm that distribution through Docker images is allowed."
    )
    st.divider()

    # --- FROM IMAGE (NO DOCKERFILE) ---
    if ptype == "from_image_without_df":
        st.info("ℹ️ This project does not require a Dockerfile.")

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Current Project", "Current Project")

        with col3:
            nav_button("Continue to README →", "README")

        st.stop()

    # --- FROM IMAGE (DOCKERFILE) ---
    if ptype == "from_image_df":
        st.toast("ℹ️ Only FROM instruction.")

    col_meta1, col_meta2 = st.columns(2)

    # contributor
    developer = col_meta1.text_input(
        "Contributor",
        value=st.session_state.projects_db[project].get("developer", "pegi3s"),
    )

    # license/terms confirmation
    agree_terms = st.checkbox(
        "I confirm that I have read and agree with the license terms stated in the Copyright header.",
        value=st.session_state.projects_db[project].get("license_confirmed", False),
    )

    st.session_state.projects_db[project]["developer"] = developer
    st.session_state.projects_db[project]["license_confirmed"] = agree_terms

    # placeholders
    if ptype == "from_image_df":
        dockerfile_placeholder = "FROM biocontainers/cd-hit:v4.6.8-2-deb_cv1\n\n"
    else:
        dockerfile_placeholder = (
            "FROM ubuntu:22.04\n\n"
            "# Install dependencies\n"
            "RUN apt update && apt install -y tool-name\n\n"
        )

    existing_code = st.session_state.projects_db[project].get("dockerfile", "")

    dockerfile_temp_key = f"dockerfile_temp_{project}"

    # initialize temp state ONLY for this project
    if dockerfile_temp_key not in st.session_state:
        st.session_state[dockerfile_temp_key] = existing_code

    docker_input = st.text_area(
        "Write Dockerfile code...",
        value=st.session_state[dockerfile_temp_key],
        height=300,
        placeholder=dockerfile_placeholder,
        key=f"docker_input_{project}"  # also make widget key project-specific
    )

    # keep changes locally (per project)
    st.session_state[dockerfile_temp_key] = docker_input

    copyright_block = """
#
#   Copyright 2018-2025 Hugo López-Fernández, Pedro M. Ferreira,
#   Miguel Reboiro-Jato, Cristina P. Vieira, and Jorge Vieira
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
"""

    docker_label = f'\n# LABEL contributor="{developer}"\n\n'
    final_dockerfile = copyright_block + docker_label + docker_input

    # --- PREVIEW GENERATE DOCKEFFILE ---
    st.subheader("Generated Dockerfile Preview")
    st.code(final_dockerfile, language="dockerfile")

    st.warning("""
    ⚠️ Make sure all required files are present in the project folder.
    Docker can only access files inside this directory during build.
    """)
    
    col_save, col_check, col_build = st.columns(3)

    apply_primary_button_style()

    # SAVE (no validation)
    saved_content = st.session_state.projects_db[project].get("dockerfile", "")
    is_dirty = mark_dirty("dockerfile", docker_input, saved_content)

    if col_save.button(
        "💾 SAVE PROGRESS",
        use_container_width=True,
        type="primary",
        disabled=not is_dirty
    ):
        if not agree_terms:
            st.error("Please confirm the license agreement before saving.")
            st.stop()

        st.session_state.projects_db[project]["dockerfile"] = docker_input
        save_local_db()
        
        mark_dirty("dockerfile", docker_input, docker_input)
        set_section_clean("dockerfile")
        st.success("Dockerfile progress saved!")

    # CHECK + VALIDATE + SAVE
    if col_check.button("✅ CHECK Dockerfile", use_container_width=True):
        
        # license confirm validation
        if not agree_terms:
            st.error("Please confirm the license agreement before finishing.")
            st.stop()

        st.divider()
        st.subheader("Dockerfile Validation")
        
        # --- FROM IMAGE (DOCKERFILE) ---
        if ptype == "from_image_df":
            validation_log = []
            issues = []

            def add_log(level, msg):
                timestamp = datetime.now().strftime("%H:%M:%S")
                validation_log.append(f"[{timestamp}] [{level.upper()}] {msg}")
                issues.append((level, msg))

            lines = [line.strip() for line in docker_input.splitlines() if line.strip()]

            # basic rules
            if len(docker_input) == 0:
                add_log("error", "Dockerfile is empty.")

            if not docker_input.startswith("FROM"):
                add_log("error", "Missing FROM instruction")

            # only FROM allowed
            forbidden = ["RUN", "COPY", "ADD", "CMD", "ENTRYPOINT", "ENV", "WORKDIR"]

            if (
                any(
                    any(line.upper().startswith(f) for f in forbidden) for line in lines
                )
                or len(lines) > 1
            ):
                add_log("error", "Only FROM instruction is allowed")

            # stop if invalid
            if issues:
                st.error("❌ Dockerfile validation failed")

                full_log = "\n".join(validation_log)

                with st.expander("📄 Validation Log"):
                    st.code(full_log)

                st.stop()

            st.success("✅ Dockerfile valid!")
            project_dir = Path(BASE_PATH) / project
            project_dir.mkdir(parents=True, exist_ok=True)
            dockerfile_path = project_dir / "Dockerfile"

            st.session_state.projects_db[project]["dockerfile"] = docker_input

            with open(dockerfile_path, "w", encoding="utf-8") as f:
                f.write(final_dockerfile)

            # update project status
            if "manual_status" in st.session_state.projects_db[project]:
                current_status_list = st.session_state.projects_db[project][
                    "manual_status"
                ]

                for row in current_status_list:
                    if row["Step"] == "Dockerfile":
                        row["Status"] = "DONE ✅"
                        row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

                done_count = sum(
                    1 for row in current_status_list if row["Status"] == "DONE ✅"
                )
                st.session_state.projects_db[project]["status"] = (
                    f"{done_count}/{len(current_status_list)} Done"
                )

            save_local_db()
            
            mark_dirty("dockerfile", docker_input, docker_input)
            set_section_clean("dockerfile")
            
            st.info(
                f"📁 Dockerfile done and saved successfully to project folder: {dockerfile_path}!"
            )

        # --- REGULAR ---
        else:
            validation_log = []
            issues = []

            def add_log(level, msg):
                timestamp = datetime.now().strftime("%H:%M:%S")
                validation_log.append(f"[{timestamp}] [{level.upper()}] {msg}")
                issues.append((level, msg))

            # Basic validation
            if not re.search(r"^FROM\s+", final_dockerfile, re.MULTILINE):
                add_log("error", "Missing FROM instruction")

            if not docker_input.strip():
                add_log("error", "Dockerfile content is empty")

            if issues:
                st.error("❌ Dockerfile validation failed")

                full_log = "\n".join(validation_log)

                with st.expander("📄 Validation Log"):
                    st.code(full_log)

                st.stop()

            # hadolint validation
            def load_hadolint_ignores():
                RULES_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/hadolint.rules"
                try:
                    context = ssl._create_unverified_context()
                    with urllib.request.urlopen(RULES_URL, context=context) as response:
                        content = response.read().decode("utf-8")

                    rules = []
                    for line in content.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("-"):
                            line = line[1:].strip()
                        rules.extend([r.strip() for r in line.split(",") if r.strip()])
                    return rules
                except:
                    return []

            def parse_hadolint_output(output):
                errors = []
                warnings = []
                info = []
                syntax_errors = []

                pattern = r"(.+?):(\d+):(\d+)\s+(DL\d+)\s+(error|warning|info):\s+(.*)"

                for line in output.splitlines():
                    line_clean = line.strip()

                    match = re.match(pattern, line_clean, re.IGNORECASE)
                    if match:
                        severity = match.group(5).lower()

                        if severity == "error":
                            errors.append(line_clean)
                        elif severity == "warning":
                            warnings.append(line_clean)
                        elif severity == "info":
                            info.append(line_clean)
                    else:
                        if "unexpected" in line_clean.lower():
                            syntax_errors.append(line_clean)
                        else:
                            if "error" in line_clean.lower():
                                errors.append(line_clean)
                            elif "warning" in line_clean.lower():
                                warnings.append(line_clean)
                            elif "info" in line_clean.lower():
                                info.append(line_clean)

                return errors, warnings, info, syntax_errors

            # run hadolint (if docker available)
            if shutil.which("docker"):
                ignore_rules = load_hadolint_ignores()

                cmd = ["docker", "run", "--rm", "-i", "hadolint/hadolint", "hadolint"]
                for rule in ignore_rules:
                    cmd.extend(["--ignore", rule])
                cmd.append("-")

                try:
                    with st.spinner("🔍 Running Hadolint..."):
                        progress = st.progress(0)

                        for i in range(30):
                            time.sleep(0.01)
                            progress.progress(i + 1)

                        result = subprocess.run(
                            cmd, input=final_dockerfile, text=True, capture_output=True
                        )

                        for i in range(30, 100):
                            time.sleep(0.005)
                            progress.progress(i + 1)

                    progress.empty()

                    output = (result.stdout or "") + "\n" + (result.stderr or "")
                    output = output.strip()

                    errors_list, warnings_list, info_list, syntax_errors = (
                        parse_hadolint_output(output)
                    )

                    for e in errors_list:
                        add_log("error", e)

                    for w in warnings_list:
                        add_log("warning", w)

                    for i in info_list:
                        add_log("info", i)

                    for s in syntax_errors:
                        add_log("error", f"Syntax: {s}")

                    if any(level == "error" for level, _ in issues):
                        st.error("❌ Validation failed")
                    elif any(level == "warning" for level, _ in issues):
                        st.warning("⚠️ Validation passed with warnings")
                    else:
                        st.success("✅ No issues found!")

                    # report
                    full_log = "\n".join(validation_log)

                    if len(full_log) != 0:
                        with st.expander("📄 Full Validation Log"):
                            st.code(full_log)

                    # SAVE ONLY IF NO ERRORS
                    if not any(level == "error" for level, _ in issues):
                        project_dir = Path(BASE_PATH) / project
                        project_dir.mkdir(parents=True, exist_ok=True)
                        dockerfile_path = project_dir / "Dockerfile"

                        st.session_state.projects_db[project]["dockerfile"] = (
                            docker_input
                        )

                        with open(dockerfile_path, "w", encoding="utf-8") as f:
                            f.write(final_dockerfile)

                        if "manual_status" in st.session_state.projects_db[project]:
                            current_status_list = st.session_state.projects_db[project][
                                "manual_status"
                            ]

                            for row in current_status_list:
                                if row["Step"] == "Dockerfile":
                                    row["Status"] = "DONE ✅"
                                    row["Date"] = datetime.now().strftime(
                                        "%Y-%m-%d %H:%M"
                                    )

                            done_count = sum(
                                1
                                for row in current_status_list
                                if row["Status"] == "DONE ✅"
                            )
                            st.session_state.projects_db[project]["status"] = (
                                f"{done_count}/{len(current_status_list)} Done"
                            )

                        save_local_db()
                        st.info(
                            f"📁 Dockerfile done and saved successfully to project folder: {dockerfile_path}!"
                        )

                except Exception as e:
                    st.error(f"Error running hadolint: {e}")

            else:
                st.info("💡 Docker not available. Cannot run hadolint.")

    project_dir = Path(BASE_PATH) / project

    # --- BUILD DOCKER IMAGE ---
    if col_build.button("🚀 Build Docker Image", use_container_width=True):
        dockerfile_path = project_dir / "Dockerfile"

        if not dockerfile_path.exists():
            st.error("Dockerfile not found in project folder.")
            st.stop()
        
        st.info("Starting Docker build...")

        success, log_lines = build_docker_image(project, project_dir)

        if success:
            st.success("✅ Docker image built successfully!")

            # UPDATE FLAGS 
            st.session_state.projects_db[project]["build_success"] = True

            # UPDATE STATUS
            if "manual_status" in st.session_state.projects_db[project]:
                for row in st.session_state.projects_db[project]["manual_status"]:
                    if row["Step"] == "Build":
                        row["Status"] = "DONE ✅"
                        row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

            save_local_db()

        else:
            st.error("❌ Build failed")
            st.code("\n".join(log_lines[-20:]))

    col1, col2, col3 = st.columns([1, 4, 1])

    with col1:
        nav_button("← Go to Current Project", "Current Project")

    with col3:
        nav_button("Continue to README →", "README")


# --- README PAGE ---
elif st.session_state.current_page == "README":
    sidebar_nave()

    project = st.session_state.active_project
    
    # validation: ensure a project is selected
    if not project:
        st.warning("Select a project first!")
        st.stop()

    p_data = st.session_state.projects_db[project]
    ptype = get_current_project_type()

    st.header(f"README Generator - {project}")

    # ----------------------------
    # TYPE BADGE
    # ----------------------------
    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
    <span style="
        background:{color};
        color:white;
        padding:5px 12px;
        border-radius:999px;
        font-size:12px;
        font-weight:600;
    ">
    {label}
    </span>
    """,
        unsafe_allow_html=True,
    )

    # --- UPDATE ---
    if ptype == "update":
        project = st.session_state.active_project
        data = st.session_state.projects_db.get(project, {})

        base = data.get("based_on")
        version = data.get("base_version")

        readme_content, readme_url, use_version = fetch_readme(base, version)

        readme_key = f"readme_{project}"

        if readme_content:
            if readme_url:
                st.caption(f"Loaded from: {readme_url}")
            
            if not use_version:
                st.warning("⚠️ This project does not follow the expected version structure (missing version folder).")

            if readme_key not in st.session_state:
                saved = data.get("readme")
                st.session_state[readme_key] = saved if saved else readme_content

            edited_readme = st.text_area("Edit your README", height=300, key=readme_key)

        else:
            st.warning("No README found in repository.")

            if readme_key not in st.session_state:
                st.session_state[readme_key] = ""

            edited_readme = st.text_area("README", height=300, key=readme_key)

        with st.expander("Preview README"):
            st.markdown(st.session_state[readme_key])

        col1, col2 = st.columns(2)

        apply_primary_button_style()

        saved = st.session_state.projects_db[project].get("readme", "")

        is_dirty = mark_dirty("readme", edited_readme, saved)
        st.session_state["readme_dirty"] = is_dirty

        # --- SAVE ONLY ---
        if col1.button("💾 SAVE PROGRESS", use_container_width=True, type="primary", disabled=not is_dirty):
            st.session_state.projects_db[project]["readme"] = edited_readme
            st.session_state.projects_db[project]["readme_raw"] = edited_readme
            save_local_db()
            mark_dirty("readme", edited_readme, edited_readme)
            set_section_clean("readme")
            st.success("README progress saved!")

        # --- CHECK README ---
        if col2.button("✅ CHECK README", use_container_width=True):
            # simple validation
            if not edited_readme or edited_readme.strip() == "":
                st.error("❌ README is empty!")

            else:
                st.success("✅ README looks good!")

                try:
                    project_dir = Path(BASE_PATH) / project
                    project_dir.mkdir(parents=True, exist_ok=True)

                    readme_path = project_dir / "README.md"

                    with open(readme_path, "w", encoding="utf-8") as f:
                        f.write(edited_readme)

                    # update DB
                    st.session_state.projects_db[project]["readme"] = edited_readme
                    st.session_state.projects_db[project]["readme_raw"] = edited_readme

                    # update project status
                    if "manual_status" in st.session_state.projects_db[project]:
                        for row in st.session_state.projects_db[project][
                            "manual_status"
                        ]:
                            if row["Step"] == "README":
                                row["Status"] = "DONE ✅"
                                row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

                    save_local_db()
                    
                    mark_dirty("readme", edited_readme, edited_readme)
                    set_section_clean("readme")

                    st.info(
                        f"📁 README done and saved successfully to project folder: {readme_path}"
                    )

                except Exception as e:
                    st.error(f"Error saving README: {e}")

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Dockerfile", "Dockerfile")

        with col3:
            nav_button("Continue to Metadata →", "Metadata")

        st.stop()

    # ----------------------------
    # GENERAL INFO
    # ----------------------------
    st.info(
        "ℹ️ Some fields are **automatically pre-filled** between README and Metadata.\n\n"
        "- README 'Tool Name' ↔ Metadata 'toolname' (normalized: lowercase, no spaces)\n"
        "- You can still edit values freely in each section.\n"
        "- Prefill only happens if the field is empty."
    )

    # ----------------------------
    # TEMPLATE SELECTION
    # ----------------------------
    TEMPLATE_URLS = {
        "regular": "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/templates/standard_README.md",
        "from_image_df": "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/templates/external_images_1_README.md", 
        "from_image_without_df": "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/templates/external_images_2_README.md"
    }

    CONFIG_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/templates/readme_config.json"  

    try:
        context = ssl._create_unverified_context()

        # ----------------------------
        # LOAD CONFIG
        # ----------------------------
        with urllib.request.urlopen(CONFIG_URL, context=context) as response:
            readme_config = json.loads(response.read().decode("utf-8"))

        ptype = get_current_project_type()

        template_map = {
            "regular": "standard",
            "from_image_df": "from_image_df",
            "from_image_without_df": "from_image_without_df",
        }

        template_key = template_map.get(ptype, "standard")

        template_cfg = readme_config["templates"][template_key]

        required_fields = template_cfg["schema_rules"].get("required_fields", [])
        conditional_fields = template_cfg["schema_rules"].get("conditional_fields", {})
        placeholder_hints = template_cfg.get("placeholder_hints", {})
        field_help = template_cfg.get("field_help", {})

        # ----------------------------
        # LOAD TEMPLATE
        # ----------------------------
        template_url = TEMPLATE_URLS.get(ptype)

        with urllib.request.urlopen(template_url, context=context) as response:
            template_text = response.read().decode("utf-8")

        placeholders = list(dict.fromkeys(re.findall(r"{{(.*?)}}", template_text)))

        # ----------------------------------------
        # DOCKERHUB FETCH (from_image_without_df)
        # ----------------------------------------
        def fetch_dockerhub_readme(url):
            try:
                repo = url.replace("https://hub.docker.com/r/", "").strip("/")
                api_url = f"https://hub.docker.com/v2/repositories/{repo}/"
                res = requests.get(api_url)
                if res.status_code == 200:
                    return res.json().get("full_description", "")
            except:
                return None

        if ptype == "from_image_without_df":
            st.subheader("DockerHub README")

            dockerhub_url = st.text_input(
                "DockerHub URL", value=p_data.get("dockerhub_url", "")
            )

            p_data["dockerhub_url"] = dockerhub_url

            # ----------------------------
            # FETCH + AUTO SAVE
            # ----------------------------
            if st.button("📥 Fetch README"):
                readme = fetch_dockerhub_readme(dockerhub_url)

                if readme:
                    p_data["dockerhub_readme"] = readme

                    project_dir = Path(BASE_PATH) / project
                    project_dir.mkdir(parents=True, exist_ok=True)

                    readme_path = project_dir / "README_dockerhub.md"

                    with open(readme_path, "w", encoding="utf-8") as f:
                        f.write(readme)

                    st.success(f"README fetched and saved to {readme_path}")

                else:
                    st.error("Failed to fetch README")

        # ----------------------------
        # SESSION STORAGE
        # ----------------------------
        if "readme_responses" not in p_data:
            p_data["readme_responses"] = {}

        saved_values = p_data["readme_responses"]
        
        meta_data = st.session_state.projects_db[project].get("meta_responses", {})

        prefill_if_empty(
            saved_values,
            meta_data,
            METADATA_TO_README,
            direction="metadata_to_readme"
        )

        # ----------------------------
        # FORM
        # ----------------------------
        with st.container():
            st.subheader("Fill README Fields")

            responses = {}
            saved_values = p_data["readme_responses"]

            for group_name, field_list in template_cfg.get("field_groups", {}).items():
                with st.expander(group_name, expanded=True):

                    cols = st.columns(2)
                    midpoint = (len(field_list) + 1) // 2

                    col1_fields = field_list[:midpoint]
                    col2_fields = field_list[midpoint:]

                    def is_field_required(p, values):
                        if p in required_fields:
                            return True

                        for field, rule in conditional_fields.items():
                            depends_on = rule.get("depends_on")
                            condition = rule.get("condition")

                            if p == field:
                                depends_val = values.get(depends_on)

                                if condition == "not_empty" and depends_val and str(depends_val).strip():
                                    return True

                            if p == depends_on:
                                target_val = values.get(field)

                                if condition == "not_empty" and target_val and str(target_val).strip():
                                    return True

                        return False

                    def render_field(p, col, col_id):
                        label = p.replace("_", " ").title()

                        is_required = is_field_required(p, saved_values)

                        display_label = f"{label} :red[*]" if is_required else label

                        if p == "docker_command":
                            subcol1, subcol2 = col.columns([4, 2])

                            value = subcol1.text_area(
                                display_label,
                                value=saved_values.get(p, ""),
                                placeholder=placeholder_hints.get(p, f"Enter {label.lower()}..."),
                                help=field_help.get(p, None),
                                height=120,
                                key=f"{p}_{col_id}",
                            )

                            with subcol2:
                                with st.popover("Suggestions", help="View command examples"):
                                    st.markdown("**Docker Command Templates**")

                                    st.caption("Quick examples you can adapt to your use case:")

                                    st.code("docker run --rm pegi3s/[tool]", language="bash")

                                    st.code(
                                        "docker run --rm -v /your/data/dir:[dir] pegi3s/[tool] -i [input]",
                                        language="bash"
                                    )

                                    st.code(
                                        "docker run --rm -v /your/data/dir:[dir] pegi3s/[tool] -i [input] -o [output]",
                                        language="bash"
                                    )

                                    st.divider()

                                    st.caption(
                                        "Replace placeholders like `[dir]`, `[tool]`, `[input]`, `[output]` with your actual values."
                                    )
                            return value

                        extra_label = ""
                        if p in ["directory_description", "input_description", "output_description"]:
                            extra_label = "  (Complete the sentence)"

                            st.markdown(f"""
                            <style>
                            textarea[aria-label="{display_label}{extra_label}"]::placeholder {{
                                color: black !important;
                                opacity: 1 !important;
                            }}
                            </style>
                            """, unsafe_allow_html=True)

                        return col.text_area(
                            f"{display_label}{extra_label}",
                            value=saved_values.get(p, ""),
                            placeholder=placeholder_hints.get(p, f"Enter {label.lower()}..."),
                            help=field_help.get(p, None),
                            height=120 if "description" in p or "command" in p else 70,
                            key=f"{p}_{col_id}",
                        )

                    for p in col1_fields:
                        responses[p] = render_field(p, cols[0], "c1")

                    for p in col2_fields:
                        responses[p] = render_field(p, cols[1], "c2")

            # placeholders already used in grouped sections
            used_placeholders = {
                p
                for fields in template_cfg.get("field_groups", {}).values()
                for p in fields
            }

            all_placeholders = re.findall(
                r"\{\{(.*?)\}\}",
                template_text
            )

            # Handle ungrouped fields automatically
            remaining_fields = [
                p
                for p in all_placeholders
                if p not in used_placeholders
            ]

            if remaining_fields:
                with st.expander("Other Fields", expanded=False):
                    cols = st.columns(2)

                    midpoint = (len(remaining_fields) + 1) // 2
                    col1_fields = remaining_fields[:midpoint]
                    col2_fields = remaining_fields[midpoint:]

                    for p in col1_fields:
                        responses[p] = render_field(p, cols[0], "extra1")

                    for p in col2_fields:
                        responses[p] = render_field(p, cols[1], "extra2")


            if responses != p_data["readme_responses"]:
                p_data["readme_responses"] = responses
                save_local_db()

            readme_manual_key = f"readme_manual_{project}"
            current_preview_text = st.session_state.get(
                readme_manual_key,
                p_data.get("readme_raw", p_data.get("readme", ""))
            )
            readme_form_already_applied = (
                p_data.get("readme_source") == "form"
                and responses == p_data.get("readme_applied_responses", {})
                and current_preview_text == p_data.get("readme_raw", "")
            )
            apply_readme_form = st.button(
                "🚀 SAVE PROGRESS",
                key=f"readme_form_save_{project}",
                disabled=readme_form_already_applied,
            )

        # ----------------------------
        # GENERATE README
        # ----------------------------
        
        current_values = p_data["readme_responses"]

        filled = template_text

        has_input = current_values.get("input_file") and str(current_values.get("input_file")).strip()
        has_output = current_values.get("output_file") and str(current_values.get("output_file")).strip()
            
        # 1. remove line input if not exist
        if not has_input:
            filled = re.sub(
                r"- `{{input_file}}` to the actual name of your .*?\n",
                "",
                filled
            )

        # 2. remove line output if not exist
        if not has_output:
            filled = re.sub(
                r"- `{{output_file}}` to the actual name of your .*?\n",
                "",
                filled
            )

        # 3. replace placeholders
        for key, val in current_values.items():
            filled = filled.replace(f"{{{{{key}}}}}", str(val))

        readme_manual_key = f"readme_manual_{project}"
        if apply_readme_form:
            p_data["readme_source"] = "form"
            p_data["readme_applied_responses"] = current_values.copy()
            p_data["readme_raw"] = filled
            st.session_state[readme_manual_key] = filled
            save_local_db()
            st.toast("README form applied to preview!")
            st.rerun()
        
        st.divider()
        
        # ----------------------------
        # PREVIEW
        # ----------------------------
        st.subheader("Final README Preview")
        
        st.warning(
            "⚠️ **Manual README Override**\n\n"
            "Changes made in the **Manual Adjustment (README)** will override the form values.\n\n"
            "- Editing the README here replaces the entire content generated from the form\n"
            "- Editing the form afterwards will overwrite the manual changes completely\n\n"
            "**Recommendation:** Use manual adjustment only for final tweaks to avoid losing changes."
        )
        
        initial_value = p_data.get("readme_raw", p_data.get("readme", ""))
        if readme_manual_key not in st.session_state:
            st.session_state[readme_manual_key] = initial_value

        final_text = st.text_area("Manual Adjustment (README)", height=400, key=readme_manual_key)
        
        if final_text != p_data.get("readme_raw", p_data.get("readme", "")):
            p_data["readme_source"] = "manual"
            p_data["readme_raw"] = final_text
        
        col1, col2 = st.columns(2)

        apply_primary_button_style()

        saved = st.session_state.projects_db[project].get("readme", "")

        if readme_form_already_applied:
            is_dirty = False
            set_section_clean("readme")
        else:
            is_dirty = mark_dirty("readme", final_text, saved)

        # --- SAVE ONLY ---
        if col1.button("💾 SAVE PROGRESS", use_container_width=True, type="primary", disabled= not is_dirty):
            st.session_state.projects_db[project]["readme"] = final_text
            st.session_state.projects_db[project]["readme_raw"] = final_text
            save_local_db()
            saved = st.session_state.projects_db[project].get("readme", "")
            is_dirty = mark_dirty("readme", final_text, saved)
            set_section_clean("readme")
            st.success("README progress saved!")

        # --- CHECK README ---
        if col2.button("✅ CHECK README", use_container_width=True):
            validation_log = []
            issues = []

            def add_log(level, msg):
                timestamp = datetime.now().strftime("%H:%M:%S")
                validation_log.append(f"[{timestamp}] [{level.upper()}] {msg}")
                issues.append((level, msg))

            # --- REQUIRED FIELDS ---
            for p in required_fields:
                val = current_values.get(p)

                if not val or str(val).strip() == "":
                    add_log("error", f"Missing required field: {p}")

            # --- CONDITIONAL FIELDS ---
            for field, rule in conditional_fields.items():
                depends_on = rule.get("depends_on")
                condition = rule.get("condition")

                depends_val = current_values.get(depends_on)
                target_val = current_values.get(field)

                # CONDITION: exists/not_empty
                if condition == "not_empty":
                    if depends_val and str(depends_val).strip():
                        if not target_val or str(target_val).strip() == "":
                            add_log(
                                "error",
                                f"{field} is required because {depends_on} is filled",
                            )

            # --- RESULT ---
            has_errors = any(level == "error" for level, _ in issues)
            has_warnings = any(level == "warning" for level, _ in issues)

            if has_errors:
                st.error("❌ README validation failed")
            elif has_warnings:
                st.warning("⚠️ README valid with warnings")
            else:
                st.success("✅ README valid!")

            # --- LOG ---
            if has_errors or has_warnings:
                with st.expander("📄 README Validation Log"):
                    st.code("\n".join(validation_log))

            # --- SAVE FILE ---
            if not has_errors:
                try:

                    project_dir = Path(BASE_PATH) / project
                    project_dir.mkdir(parents=True, exist_ok=True)

                    readme_path = project_dir / "README.md"

                    with open(readme_path, "w", encoding="utf-8") as f:
                        f.write(final_text)

                    st.session_state.projects_db[project]["readme"] = final_text
                    st.session_state.projects_db[project]["readme_raw"] = final_text

                    # UPDATE STATUS
                    if "manual_status" in st.session_state.projects_db[project]:
                        for row in st.session_state.projects_db[project][
                            "manual_status"
                        ]:
                            if row["Step"] == "README":
                                row["Status"] = "DONE ✅"
                                row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

                    save_local_db()
                    
                    mark_dirty("readme", final_text, final_text)
                    set_section_clean("readme")

                    st.info(
                        f"📁 README done and saved successfully to project folder: {readme_path}"
                    )

                except Exception as e:
                    st.error(f"Error saving README: {e}")

    except Exception as e:
        st.error(f"Error loading README system: {e}")

    col1, col2, col3 = st.columns([1, 4, 1])

    with col1:
        nav_button("← Go to Dockerfile", "Dockerfile")

    with col3:
        nav_button("Continue to Metadata →", "Metadata")


# --- METADATA PAGE (JSON GENERATOR) ---
elif st.session_state.current_page == "Metadata":
    sidebar_nave()

    project = st.session_state.active_project
    
    # validation: ensure a project is selected
    if not project:
        st.warning("Select a project first!")
        st.stop()

    ptype = get_current_project_type()

    st.header(f"📦 Metadata Generator - {project}")

    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
    <span style="
        background:{color};
        color:white;
        padding:5px 12px;
        border-radius:999px;
        font-size:12px;
        font-weight:600;
    ">
    {label}
    </span>
    """,
        unsafe_allow_html=True,
    )

    # --- UPDATE ---
    if ptype == "update":
        project = st.session_state.active_project
        data = st.session_state.projects_db.get(project, {})

        base = data.get("based_on")
        version = data.get("base_version")

        metadata = get_project_metadata(base)  

        if metadata:
            with st.expander("View metadata (latest version)"):
                st.json(metadata)

        else:
            st.warning("No metadata found.")

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Current README", "README")

        with col3:
            nav_button("Continue to Ontology →", "Ontology")

        st.stop()
    
    # --- GENERAL INFO ---
    st.info(
        "ℹ️ Some fields are **automatically pre-filled** between README and Metadata.\n\n"
        "- README 'Tool Name' ↔ Metadata 'toolname' (normalized: lowercase, no spaces)\n"
        "- You can still edit values freely in each section.\n"
        "- Prefill only happens if the field is empty."
    )

    # --- 1. CONFIGURATIONS ---
    TEMPLATE_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/refs/heads/master/metadata/tools/templates/template_metadata.json"  
    GLOBAL_META_URL = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/metadata.json"

    FORCED_EMPTY_LISTS = {
        "bug_found",
        "not_working",
        "no_longer_tested",
        "comments",
        "input_files",
        "input_data_type",
        "usual_invocation_specific_comments",
    }

    if "meta_responses" not in st.session_state.projects_db[project]:
        st.session_state.projects_db[project]["meta_responses"] = {}

    # --- 2. LOADING TEMPLATE & GLOBAL FILE ---
    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(TEMPLATE_URL, context=context) as response:
            full_schema = json.loads(response.read().decode("utf-8"))

        with urllib.request.urlopen(GLOBAL_META_URL, context=context) as response:
            global_metadata = json.loads(response.read().decode("utf-8"))

        template_obj = full_schema.get("template", {})
        schema_rules = full_schema.get("schema_rules", {})
        required_fields = schema_rules.get("required_fields", [])
        FIELD_GUIDE = full_schema.get("placeholder_hints", {})
        GROUPS = full_schema.get("field_groups", {})
        FIELD_HELP = full_schema.get("field_help", {})
        hidden_fields = set(schema_rules.get("hidden_fields", []))

        template_json_text = json.dumps(template_obj)
        placeholders = list(dict.fromkeys(re.findall(r"{{(.*?)}}", template_json_text)))

        # --- SAFE PLACEHOLDER REPLACEMENT ---
        def replace_placeholders(obj, values):
            if isinstance(obj, dict):
                return {k: replace_placeholders(v, values) for k, v in obj.items()}

            elif isinstance(obj, list):
                return [replace_placeholders(i, values) for i in obj]

            elif isinstance(obj, str):
                matches = re.findall(r"{{(.*?)}}", obj)
                if not matches:
                    return obj
                if obj.strip() == f"{{{{{matches[0]}}}}}":
                    return values.get(matches[0], "")
                for m in matches:
                    if m in values:
                        obj = obj.replace(f"{{{{{m}}}}}", str(values[m]))
                return obj
            return obj

        # --- 3. FORM ---
        with st.container():
            st.subheader("Tool Metadata Information")
            metadata_responses = {}
            saved_values = st.session_state.projects_db[project]["meta_responses"]

            readme_data = st.session_state.projects_db[project].get("readme_responses", {})

            prefill_if_empty(
                saved_values,
                readme_data,
                README_TO_METADATA,
                direction="readme_to_metadata"
            )

            # Get all placeholders dynamically
            template_json_text = json.dumps(template_obj)
            all_placeholders = list(
                dict.fromkeys(re.findall(r"{{(.*?)}}", template_json_text))
            )

            # Track which ones are used
            used_placeholders = set()

            for group_name, field_list in GROUPS.items():
                with st.expander(group_name, expanded=True):
                    current_group_fields = [
                        p
                        for p in field_list
                        if p in all_placeholders and p not in hidden_fields
                    ]
                    used_placeholders.update(current_group_fields)

                    midpoint = (len(current_group_fields) + 1) // 2
                    col1_fields = current_group_fields[:midpoint]
                    col2_fields = current_group_fields[midpoint:]

                    cols = st.columns(2)

                    def render_field(p, col, col_id):
                        def format_label(p):
                            label = p.replace("_", " ").title()
                            label = label.replace("Gui", "GUI")
                            return label
                        
                        label_base = format_label(p)
                        
                        display_label = (
                            f"{label_base} :red[*]"
                            if p in required_fields
                            else label_base
                        )
                        current_val = saved_values.get(p, "")
                        placeholder_text = FIELD_GUIDE.get(
                            p, f"Enter {label_base.lower()}..."
                        )
                        
                        help_text = FIELD_HELP.get(p, None)
                        
                        if p == "recommended_version_date":
                            return col.date_input(
                                display_label,
                                value=datetime.today(),
                                key=f"{p}_{col_id}"
                            ).strftime("%d/%m/%Y")

                        return col.text_input(
                            display_label,
                            value=str(current_val),
                            placeholder=placeholder_text,
                            help=help_text,
                            key=f"{p}_{col_id}",
                        )

                    # Column 1
                    for p in col1_fields:
                        metadata_responses[p] = render_field(p, cols[0], "col1")

                    # Column 2
                    for p in col2_fields:
                        metadata_responses[p] = render_field(p, cols[1], "col2")

            # Handle ungrouped fields automatically
            remaining_fields = [
                p
                for p in all_placeholders
                if p not in used_placeholders and p not in hidden_fields
            ]

            if remaining_fields:
                with st.expander("Other Fields", expanded=False):
                    cols = st.columns(2)

                    midpoint = (len(remaining_fields) + 1) // 2
                    col1_fields = remaining_fields[:midpoint]
                    col2_fields = remaining_fields[midpoint:]

                    for p in col1_fields:
                        metadata_responses[p] = render_field(p, cols[0], "extra1")

                    for p in col2_fields:
                        metadata_responses[p] = render_field(p, cols[1], "extra2")

            # --- SUBMIT ---
            if metadata_responses != st.session_state.projects_db[project]["meta_responses"]:
                st.session_state.projects_db[project]["meta_responses"] = metadata_responses
                save_local_db()

            metadata_raw_key = f"metadata_json_raw_{project}"
            current_metadata_preview_text = st.session_state.get(
                metadata_raw_key,
                st.session_state.projects_db[project].get("metadata_json_raw", "")
            )
            metadata_form_already_applied = (
                st.session_state.projects_db[project].get("metadata_source") == "form"
                and metadata_responses == st.session_state.projects_db[project].get("metadata_applied_responses", {})
                and current_metadata_preview_text == st.session_state.projects_db[project].get("metadata_json_raw", "")
            )
            apply_metadata_form = st.button(
                "🚀 SAVE PROGRESS",
                key=f"metadata_form_save_{project}",
                disabled=metadata_form_already_applied,
            )

            if apply_metadata_form:
                missing = [
                    p
                    for p in required_fields
                    if not metadata_responses.get(p)
                ]

                if missing:
                    st.warning(
                        f"Please fill all required fields: {', '.join([m.replace('_', ' ').title() for m in missing])}"
                    )

                def extract_input_files(invocation):
                    if not invocation:
                        return []

                    # procura caminhos tipo /data/test/data/ficheiro.ext
                    matches = re.findall(r"/data/test/data/([^\s]+)", invocation)

                    return matches

                processed_values = {}
                for k, v in metadata_responses.items():
                    if k in FORCED_EMPTY_LISTS:
                        if not v or str(v).strip() == "":
                            processed_values[k] = []
                        else:
                            cleaned_list = [x.strip() for x in str(v).split(",") if x.strip()]
                            processed_values[k] = cleaned_list if cleaned_list else []

                    elif isinstance(v, str):
                        cleaned = v.strip()

                        if k == "invocation_general":
                            processed_values[k] = cleaned + " " if cleaned else ""

                        elif k in {"usual_invocation_specific", "test_invocation"}:
                            processed_values[k] = cleaned

                        else:
                            processed_values[k] = cleaned

                    else:
                        processed_values[k] = v

                    # --- AUTO GENERATE TEST URLS ---
                    invocation = processed_values.get("test_invocation", "")
                    input_files = extract_input_files(invocation)

                    BASE_INPUT_URL = "http://evolution6.i3s.up.pt/static/pegi3s/dockerfiles/input_test_data/"
                    BASE_OUTPUT_URL = "http://evolution6.i3s.up.pt/static/pegi3s/dockerfiles/output_test_data/"

                    if input_files:
                        if len(input_files) == 1:
                            # single file
                            processed_values["test_data_url"] = (
                                BASE_INPUT_URL + input_files[0]
                            )
                        else:
                            # multiple files → zip (use tool name or fallback)
                            tool_name = processed_values.get("tool_name", "dataset")
                            processed_values["test_data_url"] = (
                                BASE_INPUT_URL + f"{tool_name}.zip"
                            )

                    # output → zip 
                    tool_name = processed_values.get("tool_name", "output")
                    processed_values["test_results_url"] = (
                        BASE_OUTPUT_URL + f"{tool_name}_output.zip"
                    )
                    
                # AUTO GUI FLAG
                gui_cmd = processed_values.get("gui_command", "").strip()
                processed_values["gui"] = bool(gui_cmd)
                processed_values["status"] = "Usable"
                
                # Garantir campos hidden obrigatórios
                for field in FORCED_EMPTY_LISTS:

                    if field not in processed_values:
                        processed_values[field] = []

                applied_metadata_preview = False
                try:
                    parsed = replace_placeholders(template_obj, processed_values)
                    
                    def clean_empty_lists(obj):
                        if isinstance(obj, dict):
                            return {k: clean_empty_lists(v) for k, v in obj.items()}
                        elif isinstance(obj, list):
                            cleaned = [clean_empty_lists(i) for i in obj if i not in ("", [], None)]
                            return cleaned
                        return obj

                    parsed = clean_empty_lists(parsed)
                    
                    raw_preview = json.dumps(parsed, indent=4, ensure_ascii=False)
                    metadata_raw_key = f"metadata_json_raw_{project}"

                    st.session_state.projects_db[project]["metadata_preview_json"] = parsed
                    st.session_state.projects_db[project]["metadata_json_raw"] = raw_preview
                    st.session_state.projects_db[project]["metadata_source"] = "form"
                    st.session_state.projects_db[project]["metadata_applied_responses"] = metadata_responses.copy()
                    st.session_state.projects_db[project].pop("metadata_invalid_raw", None)
                    st.session_state[metadata_raw_key] = raw_preview
                    st.toast("Metadata form applied to preview!")
                    applied_metadata_preview = True
                except Exception as e:
                    st.error(f"Error parsing final JSON: {e}")

                save_local_db()
                if applied_metadata_preview:
                    st.rerun()

    except Exception as e:
        st.error(f"Synchronization/Loading Error: {e}")

    st.divider()


    # --- 4. PREVIEW & FINAL SAVE ---
    current_meta_obj = st.session_state.projects_db[project].get(
        "metadata_preview_json",
        st.session_state.projects_db[project].get("metadata_json", {})
    )
    if current_meta_obj:        
        st.subheader("Final Metadata Preview")
        st.warning(
            "⚠️ **Manual JSON Override**\n\n"
            "Changes made in the **Manual Adjustment (JSON)** will override the form values.\n\n"
            "- Editing the JSON will overwrite the corresponding fields from the form\n"
            "- Editing the form afterwards will overwrite the entire Manual Adjustment\n\n"
            "**Recommendation:** Use manual adjustment only for final tweaks to avoid losing changes."
        )
        
        json_display = st.session_state.projects_db[project].get(
            "metadata_invalid_raw",
            st.session_state.projects_db[project].get(
                "metadata_json_raw",
                json.dumps(current_meta_obj, indent=4)
            )
        )
        
        metadata_raw_key = f"metadata_json_raw_{project}"
        if st.session_state.projects_db[project].get("metadata_invalid_raw"):
            st.session_state[metadata_raw_key] = st.session_state.projects_db[project]["metadata_invalid_raw"]
        elif metadata_raw_key not in st.session_state:
            st.session_state[metadata_raw_key] = json_display

        edited_json = st.text_area(
            "Manual adjustment (JSON):", height=400, key=metadata_raw_key
        )

        previous_metadata_raw = st.session_state.projects_db[project].get("metadata_json_raw", "")
        if edited_json != previous_metadata_raw:
            st.session_state.projects_db[project]["metadata_source"] = "manual"

        st.session_state.projects_db[project]["metadata_json_raw"] = edited_json

        col_save, col_check = st.columns(2)

        apply_primary_button_style()

        try:
            parsed_current = json.loads(edited_json)
            current = json.dumps(parsed_current, sort_keys=True)
        except:
            current = edited_json.strip()

        saved = json.dumps(
            st.session_state.projects_db[project].get("metadata_json", {}),
            sort_keys=True
        )

        if metadata_form_already_applied:
            is_dirty = False
            set_section_clean("metadata")
        else:
            is_dirty = mark_dirty("metadata", current, saved)
        
        if col_save.button(
            "💾 SAVE PROGRESS", use_container_width=True, type="primary", disabled= not is_dirty
        ):
            raw_json = st.session_state.projects_db[project].get("metadata_json_raw", "")

            try:
                parsed = json.loads(raw_json)

                st.session_state.projects_db[project]["metadata_json"] = parsed
                st.session_state.projects_db[project]["metadata_preview_json"] = parsed
                st.session_state.projects_db[project]["metadata_json_raw"] = raw_json
                st.session_state.projects_db[project].pop("metadata_invalid_raw", None)
            
                save_local_db()
                
                normalized = json.dumps(parsed, sort_keys=True)

                mark_dirty("metadata", normalized, normalized)
                set_section_clean("metadata")
                
                st.success("Metadata progress saved!")
            
            except json.JSONDecodeError:
                st.error(
                    "❌ Cannot save — JSON is invalid. Please fix syntax errors before saving."
                )

        if col_check.button("✅ CHECK METADATA", use_container_width=True):
            log_lines = []

            try:
                # 1. Load the edited JSON
                final_data = json.loads(edited_json)

                # 2. Advanced Mapping: Extract placeholders even inside prefixed strings
                # Example: "term={{pubmed}}" -> mapping["pubmed"] = "pubmed"
                mapping = {}

                def build_mapping(obj):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, str):
                                found = re.findall(r"{{(.*?)}}", v)
                                for p_name in found:
                                    mapping[p_name] = k
                            else:
                                build_mapping(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            build_mapping(item)

                build_mapping(template_obj)

                # 3. Deep search for values and prefix validation
                def check_value_deep(obj, target_key, p_name):
                    if isinstance(obj, dict):
                        if target_key in obj:
                            val = obj[target_key]
                            # Validation logic:
                            # If it's a search field like pubmed, check if user added content after 'term='
                            if p_name in [
                                "pubmed",
                                "scholar",
                                "bioprotocol",
                                "bioprotocol_exchange",
                            ]:
                                prefixes = ["term=", "q=", "content="]
                                if any(val == pref for pref in prefixes) or val == "":
                                    return "EMPTY"
                                return "OK"

                            # Standard empty checks
                            if val in [None, "", [], [""]]:
                                return "EMPTY"
                            return "OK"

                        for v in obj.values():
                            res = check_value_deep(v, target_key, p_name)
                            if res != "NOT_FOUND":
                                return res
                    elif isinstance(obj, list):
                        for item in obj:
                            res = check_value_deep(item, target_key, p_name)
                            if res != "NOT_FOUND":
                                return res
                    return "NOT_FOUND"

                # 4. Final Validation Loop
                missing_fields = []
                for p in required_fields:
                    real_key = mapping.get(p, p)
                    status = check_value_deep(final_data, real_key, p)

                    if status in ["EMPTY", "NOT_FOUND"]:
                        missing_fields.append(f"{real_key.replace('_', ' ').title()}")

                if missing_fields:
                    if missing_fields:
                        st.error("❌ Validation Failed")
                        log_lines.append(f"Missing fields: {', '.join(missing_fields)}")

                        full_log = "\n".join(log_lines)

                        with st.expander("📄 Metadata Check Log"):
                            st.code(full_log)

                else:
                    # 5. Success
                    st.session_state.projects_db[project]["metadata_json"] = final_data
                    st.session_state.projects_db[project]["metadata_preview_json"] = final_data
                    
                    raw_json = json.dumps(final_data, indent=4, ensure_ascii=False)

                    st.session_state.projects_db[project]["metadata_json_raw"] = raw_json
                    st.session_state.projects_db[project].pop("metadata_invalid_raw", None)

                    mark_dirty("metadata", raw_json, raw_json)
                    set_section_clean("metadata")

                    try:

                        project_dir = Path(BASE_PATH) / project
                        project_dir.mkdir(parents=True, exist_ok=True)

                        metadata_path = project_dir / "metadata.json"

                        with open(metadata_path, "w", encoding="utf-8") as f:
                            json.dump(final_data, f, indent=4, ensure_ascii=False)

                        st.success(
                            "✅ **Metadata Verified!** All required fields are valid."
                        )
                        st.success(
                            f"📁 Metadata saved to project folder: {metadata_path}"
                        )

                    except Exception as e:
                        st.error(f"Error saving metadata file: {e}")

                    if "manual_status" in st.session_state.projects_db[project]:
                        current_status_list = st.session_state.projects_db[project][
                            "manual_status"
                        ]
                        for row in current_status_list:
                            if row["Step"] == "Metadata":
                                row["Status"] = "DONE ✅"
                                row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

                        done_count = sum(
                            1
                            for row in current_status_list
                            if row["Status"] == "DONE ✅"
                        )
                        st.session_state.projects_db[project]["status"] = (
                            f"{done_count}/{len(current_status_list)} Done"
                        )

                    save_local_db()

            except json.JSONDecodeError:
                st.error(
                    "❌ **Invalid JSON syntax!** Please check for missing commas or quotes."
                )
            except Exception as e:
                st.error(f"❌ **Check Error:** {e}")

    col1, col2, col3 = st.columns([1, 4, 1])

    with col1:
        nav_button("← Go to README", "README")

    with col3:
        nav_button("Continue to Ontology →", "Ontology")


# --- ONTOLOGY PAGE ---
elif st.session_state.current_page == "Ontology":
    sidebar_nave()

    project = st.session_state.active_project
    
    # validation: ensure a project is selected
    if not project:
        st.warning("Select a project first!")
        st.stop()

    ptype = get_current_project_type()

    st.header(f"🧬 DIO Ontology - {project}")

    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
    <span style="
        background:{color};
        color:white;
        padding:5px 12px;
        border-radius:999px;
        font-size:12px;
        font-weight:600;
    ">
    {label}
    </span>
    """,
        unsafe_allow_html=True,
    )

    # --- UPDATE ---
    if ptype == "update":
        project = st.session_state.active_project
        data = st.session_state.projects_db.get(project, {})

        base = data.get("based_on")

        ontology, relations, diaf_data = get_remote_dio_data()

        # filter
        related_ids = [
            item["id"] for item in diaf_data if item["tool"].lower() == base.lower()
        ]

        st.markdown(
            """
        <style>
        .ontology-card {
            border: 1px solid #e5e7eb;
            border-left: 6px solid;
            border-radius: 12px;
            padding: 12px 16px;
            margin-bottom: 12px;
            background-color: #fafafa;
        }
        .ontology-title {
            font-size: 18px;
            font-weight: 600;
        }
        .ontology-id {
            font-size: 12px;
            color: #6b7280;
        }
        </style>
        """,
            unsafe_allow_html=True,
        )

        def get_style_from_path(path):
            if not path:
                return "#9ca3af"

            return "#6b7280"

        if related_ids:
            for oid in related_ids:
                name = ontology.get(oid, "Unknown")
                path = get_ontology_path(oid, ontology, relations)

                color = get_style_from_path(path)

                st.markdown(
                    f"""
                    <div class="ontology-card" style="border-left-color: {color};">
                        <div class="ontology-title">{name}</div>
                        <div class="ontology-id">{oid}</div>
                        {"<div style='margin-top:8px;'>🧬 <b>Path:</b> " + path + "</div>" if path else ""}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        else:
            st.warning("No ontology terms found for this tool.")

        st.markdown("<br>", unsafe_allow_html=True)

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Metadata", "Metadata")

        with col3:
            nav_button("Continue to Build and Test →", "Build and Test")

        st.stop()

    # --- LOAD FROM GITHUB ONLY ---
    base_obo_content = ""
    dio_terms_map = {}
    dio_relations = {}
    existing_ids = set()

    try:
        context = ssl._create_unverified_context()

        obo_url = "https://raw.githubusercontent.com/pegi3s/dockerfiles/master/metadata/dio.obo"
        with urllib.request.urlopen(obo_url, context=context) as response:
            base_obo_content = response.read().decode("utf-8")

        # st.info("Loaded ontology from GitHub.")

    except Exception as e:
        st.error(f"Error fetching ontology: {e}")
        st.stop()

    # --- PARSE OBO ---
    if base_obo_content:
        current_id = None

        for line in base_obo_content.splitlines():
            if line.startswith("id:"):
                current_id = line.split("id:")[1].strip()
                existing_ids.add(current_id)

            elif line.startswith("name:") and current_id:
                dio_terms_map[current_id] = line.split("name:")[1].strip()

            elif line.startswith("is_a:") and current_id:
                parent_id = line.split("is_a:")[1].split("!")[0].strip()
                dio_relations[current_id] = parent_id

        children = set(dio_relations.keys())
        parents = set(dio_relations.values())
        leaf_terms = children - parents

    # --- BUILD TREE ---
    hierarchical_options = {}
    for tid in dio_terms_map:
        path = get_ontology_path(tid, dio_terms_map, dio_relations)
        hierarchical_options[tid] = f"{tid}: {path}"

    st.subheader(f"Select Ontology Terms - {project}")

    # --- FILTER ---
    branch_search = st.text_input(
        "🔍 Filter (e.g., 'DNA', 'Sequences'). Please view and select the options below:"
    )

    selected_terms = st.session_state.projects_db[project].get("ontology_terms", [])

    if branch_search:
        display_options = {
            k: v
            for k, v in hierarchical_options.items()
            if branch_search.lower() in v.lower()
        }
    else:
        display_options = {
            k: v for k, v in hierarchical_options.items() if k in leaf_terms
        }

    st.markdown(
        """
    <style>

    div[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
        background-color: #2563eb !important;
        color: white !important;
    }

    div[data-testid="stMultiSelect"] span[data-baseweb="tag"] span {
        color: white !important;
    }

    div[data-testid="stMultiSelect"] span[data-baseweb="tag"] svg {
        fill: white !important;
    }

    /* hover */
    div[data-testid="stMultiSelect"] span[data-baseweb="tag"]:hover {
        background-color: #2563eb !important;
    }

    </style>
    """,
        unsafe_allow_html=True,
    )
    
    st.markdown(
        """
    <style>
    .ontology-term-row {
        border: 1px solid #e5e7eb;
        border-left: 4px solid #d1d5db;
        border-radius: 8px;
        padding: 10px 12px;
        margin: 0 0 8px 0;
        background: #ffffff;
    }
    .ontology-term-row.selected {
        border-color: #86efac;          
        border-left-color: #22c55e;     
        background: #f0fdf4;            
    }
    .ontology-term-title {
        font-weight: 650;
        color: #111827;
        line-height: 1.25;
    }
    .ontology-term-id {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        font-size: 12px;
        color: #4b5563;
        margin-top: 2px;
    }
    .ontology-term-path {
        color: #6b7280;
        font-size: 13px;
        line-height: 1.35;
        margin-top: 6px;
        margin-bottom: 12px;
    }
    .ontology-term-badge {
        display: inline-block;
        border-radius: 999px;
        padding: 3px 8px;
        font-size: 12px;
        font-weight: 650;
        background: #dcfce7;
        color: #166534;
        border: 1px solid #86efac;
        white-space: nowrap;
    }
    .ontology-term-empty-badge {
        color: #9ca3af;
        font-size: 12px;
        white-space: nowrap;
    }
    </style>
    """,
        unsafe_allow_html=True,
    )
    
    st.markdown("""
    <style>

    span:has(+ input[type="checkbox"]:checked) {
        background-color: #16a34a !important;
        border-color: #16a34a !important;
    }

    span:has(+ input[type="checkbox"]:checked) svg {
        stroke: white !important;
    }

    /* hover */
    span:has(+ input[type="checkbox"]):hover {
        border-color: #16a34a !important;
    }

    </style>
    """, unsafe_allow_html=True)
    
    current_selected_count = sum(
        1
        for oid in display_options
        if st.session_state.get(f"ontology_{oid}", oid in selected_terms)
    )
    st.caption(f"{len(display_options)} terminal ontology terms shown - {current_selected_count} currently selected")

    selected_mappings = []

    for oid, label in display_options.items():
        checkbox_key = f"ontology_{oid}"
        checked = st.session_state.get(checkbox_key, oid in selected_terms)

        path = label.split(": ", 1)[1] if ": " in label else label
        term_name = dio_terms_map.get(oid, path.split(" > ")[-1] if " > " in path else path)
        with st.container(border=True):
            col1, col2, col3 = st.columns([0.08, 0.76, 0.16], vertical_alignment="center")

            with col1:
                value = st.checkbox(
                    "Select ontology term",
                    value=checked,
                    key=checkbox_key,
                    label_visibility="collapsed",
                )

            with col2:
                st.markdown(
                    f"""
                    <div class="ontology-term-title">{html.escape(term_name)}</div>
                    <div class="ontology-term-id">{html.escape(oid)}</div>
                    <div class="ontology-term-path">{html.escape(path)}</div>
                    """,
                    unsafe_allow_html=True,
                )

            with col3:
                if value:
                    st.markdown('<span class="ontology-term-badge">Selected</span>', unsafe_allow_html=True)
                else:
                    st.markdown('<span class="ontology-term-empty-badge">Available</span>', unsafe_allow_html=True)

        if value:
            selected_mappings.append(oid)

    # --- SUGGESTIONS ---
    st.divider()
    st.subheader("💡 Suggest New Ontology Terms")

    new_term = st.text_input("New term suggestion")

    if st.button("➕ Add suggestion"):
        if new_term.strip():
            suggestions = st.session_state.projects_db[project].get(
                "ontology_suggestions", []
            )

            if new_term.strip() not in suggestions:
                suggestions.append(new_term.strip())
                st.session_state.projects_db[project]["ontology_suggestions"] = (
                    suggestions
                )
                st.success("Suggestion added!")
            else:
                st.warning("Already added")

    suggestions = st.session_state.projects_db[project].get("ontology_suggestions", [])

    if suggestions:
        st.write("### Suggested terms")
        for i, s in enumerate(suggestions):
            col1, col2 = st.columns([5, 1])
            col1.code(s)
            if col2.button("❌", key=f"rm_{i}"):
                suggestions.pop(i)
                st.session_state.projects_db[project]["ontology_suggestions"] = (
                    suggestions
                )
                st.rerun()

    # --- FUNCTION TO SAVE DIAF ---
    def generate_diaf(project):
        data = st.session_state.projects_db[project]

        terms = data.get("ontology_terms", [])
        suggs = data.get("ontology_suggestions", [])

        project_dir = Path(BASE_PATH) / project
        project_dir.mkdir(parents=True, exist_ok=True)

        path = project_dir / "ontology.diaf"

        lines = []

        lines.append("# Ontology terms")
        for t in terms:
            lines.append(t)

        if suggs:
            lines.append("\n# Suggestions")
            for s in suggs:
                lines.append(f"SUGGESTION: {s}")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return path

    # --- ACTION BUTTONS ---
    col1, col2 = st.columns(2)

    apply_primary_button_style()

    saved_terms = st.session_state.projects_db[project].get("ontology_terms", [])
    is_dirty = mark_dirty("ontology", selected_mappings, saved_terms)

    # SAVE
    if col1.button("💾 SAVE PROGRESS", use_container_width=True, type="primary", disabled=not is_dirty):
        st.session_state.projects_db[project]["ontology_terms"] = selected_mappings
        save_local_db()
        set_section_clean("ontology")
        st.success("Ontology progress saved!")

    # CHECK + SAVE DIAF
    if col2.button("✅ CHECK ONTOLOGY", use_container_width=True):
        if not selected_mappings:
            st.error("Select at least one ontology term.")
            st.stop()

        st.session_state.projects_db[project]["ontology_terms"] = selected_mappings

        diaf_path = generate_diaf(project)

        # update status
        if "manual_status" in st.session_state.projects_db[project]:
            for row in st.session_state.projects_db[project]["manual_status"]:
                if row["Step"] == "Ontology":
                    row["Status"] = "DONE ✅"
                    row["Date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

            done_count = sum(
                1
                for row in st.session_state.projects_db[project]["manual_status"]
                if row["Status"] == "DONE ✅"
            )

            st.session_state.projects_db[project]["status"] = (
                f"{done_count}/{len(st.session_state.projects_db[project]['manual_status'])} Done"
            )

        save_local_db()
        set_section_clean("ontology")

        st.success("✅ Ontology valid!")
        st.info(
            f"📁 Ontology done and saved successfully to project folder: {diaf_path}"
        )

    # --- AI ASSISTANT ---
    st.divider()
    st.subheader("🪄 AI Ontology Assistant")

    readme_text = st.session_state.projects_db[project].get("readme", "")

    leaf_preview = "\n".join(
        [
            f"{tid}: {hierarchical_options[tid].split(': ', 1)[1]}"
            for tid in leaf_terms
            if tid in hierarchical_options
        ]
    )

    gemini_prompt = f"""
You are an expert in bioinformatics ontologies.

Tool name:
{project}

Tool description:
{readme_text}

AVAILABLE DIO TERMINAL TERMS:
{leaf_preview}

Task:
Suggest the most appropriate ontology IDs for this tool.

Important:
- Only use IDs from the provided list
- Prefer the most specific terminal ontology terms

Return:
- Ontology ID
- Ontology name
- Short explanation
"""

    st.caption("Prompt")

    components.html(f"""
    <style>
    .container {{
        font-family: "Source Sans Pro", sans-serif;
    }}

    textarea {{
        width: 95%;
        height: 200px;
        padding: 0.75rem;
        border-radius: 0.5rem;
        border: 1px solid #d1d5db;
        font-size: 14px;
        resize: vertical;
        outline: none;
    }}

    textarea:focus {{
        border-color: #2563eb;
        box-shadow: 0 0 0 1px #2563eb;
    }}

    .copy-btn {{
        margin-top: 10px;
        background-color: #f3f4f6;
        border: 1px solid #d1d5db;
        padding: 8px 12px;
        border-radius: 0.5rem;
        cursor: pointer;
        font-weight: 500;
    }}

    .copy-btn:hover {{
        background-color: #e5e7eb;
    }}

    .copy-btn:active {{
        background-color: #d1d5db;
    }}
    </style>

    <div class="container">
        <textarea id="prompt">{gemini_prompt}</textarea>
        <br>
        <button class="copy-btn" onclick="copyText()">📋 Copy Prompt</button>
    </div>

    <script>
    function copyText() {{
        const text = document.getElementById("prompt");
        text.select();
        document.execCommand("copy");
    }}
    </script>
    """, height=280)

    st.link_button("🌐 Open Gemini", "https://gemini.google.com/")

    # BACK
    col1, col2, col3 = st.columns([1, 4, 1])

    with col1:
        nav_button("← Go to Metadata", "Metadata")

    with col3:
        nav_button("Continue to Build and Test →", "Build and Test")


# --- TEST DATA PAGE ---
elif st.session_state.current_page == "Build and Test":
    sidebar_nave()

    project = st.session_state.active_project
    
    # validation: ensure a project is selected
    if not project:
        st.warning("Select a project first!")
        st.stop()

    ptype = get_current_project_type()

    st.header(f"⚙️ Build and Test - {project}")

    p_data = st.session_state.projects_db[project]

    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
    <span style="
        background:{color};
        color:white;
        padding:5px 12px;
        border-radius:999px;
        font-size:12px;
        font-weight:600;
    ">
    {label}
    </span>
    """,
        unsafe_allow_html=True,
    )

    # --- UPDATE ---
    if ptype == "update":
        project = st.session_state.active_project
        data = st.session_state.projects_db.get(project, {})

        base = data.get("based_on")
        version = data.get("base_version")

        project_dir = Path(BASE_PATH) / project
        data_dir = project_dir / "test_data"
        input_dir = data_dir / "data"
        output_dir = data_dir / "results"

        project_dir.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        # FETCH REPO 
        def fetch_repo_version(repo_url, project, version, project_dir):

            with tempfile.TemporaryDirectory() as tmpdir:
                subprocess.run(["git", "clone", repo_url, tmpdir], check=True)

                repo_path = Path(tmpdir)

                possible_paths = [
                    repo_path / project / version,  # with version folder
                    repo_path / project,  # without version folder
                ]

                sub_path = None

                for path in possible_paths:
                    if path.exists():
                        sub_path = path
                        break

                if not sub_path:
                    raise Exception(f"{project}/{version} not found in repo")

                protected_files = {"Dockerfile", "README.md"}

                for item in sub_path.iterdir():
                    if item.name in protected_files:
                        continue

                    dst = project_dir / item.name

                    if item.is_file():
                        shutil.copy(item, dst)

                    elif item.is_dir():
                        shutil.copytree(item, dst, dirs_exist_ok=True)

        # -------------------------------------------------
        # BUILD IMAGE
        # -------------------------------------------------

        st.subheader("Build Image")

        st.info("""
        Extra files are fetched from GitHub.
        """)

        if st.button("🚀 Build Docker Image"):
            repo_url = "https://github.com/pegi3s/dockerfiles"

            if not version:
                st.error("Version not defined")
                st.stop()

            dockerfile_path = project_dir / "Dockerfile"

            if not dockerfile_path.exists():
                st.error("Local Dockerfile missing")
                st.stop()

            try:
                with st.spinner("Fetching extra files from GitHub repository..."):
                    fetch_repo_version(repo_url, base, version, project_dir)
                st.success("Repository extra files loaded (if existing)!")

            except Exception as e:
                st.error(f"Error fetching repo: {e}")
                st.stop()

            st.info("Starting Docker build...")

            success, log_lines = build_docker_image(project, project_dir)
            
            if success:
                st.success("✅ Docker image built successfully!")
                st.session_state.projects_db[project]["build_success"] = True

                if "manual_status" in st.session_state.projects_db[project]:
                    current_status_list = st.session_state.projects_db[project][
                        "manual_status"
                    ]

                    for row in current_status_list:
                        if row["Step"] == "Build":
                            row["Status"] = "DONE ✅"
                            row["Date"] = datetime.now().strftime(
                                "%Y-%m-%d %H:%M"
                            )

                    done_count = sum(
                        1
                        for row in current_status_list
                        if row["Status"] == "DONE ✅"
                    )
                    st.session_state.projects_db[project]["status"] = (
                        f"{done_count}/{len(current_status_list)} Done"
                    )
            
                save_local_db()

            else:
                st.error("❌ Build failed")
                st.code("\n".join(log_lines[-20:]))

        st.divider()

        # -------------------------------------------------
        # TEST IMAGE
        # -------------------------------------------------

        st.subheader("Test Image")

        def build_command_from_metadata(metadata, project):
            general = metadata.get("invocation_general", "")
            test_specific = metadata.get("test_invocation_specific", "")
            return f"{general} {test_specific}".strip()

        metadata = get_project_metadata(base)

        if st.button("🔎 Load command from metadata"):
            cmd = build_command_from_metadata(metadata, base)

            if cmd:
                st.session_state.run_command = cmd
                # st.success("Command loaded")
            else:
                st.warning("Missing invocation_general")

        st.caption(
            f"Inputs → /data/{project}/test_data/data | Outputs → /data/{project}/test_data/results"
        )

        run_command = st.text_area(
            "Docker Command", height=120, value=st.session_state.get("run_command", "")
        )

        # -------------------------------------------------
        # DOWNLOAD TEST DATA
        # -------------------------------------------------

        def download_test_data(url, input_dir):

            response = requests.get(url)

            if response.status_code != 200:
                raise Exception("Download failed")

            if url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                    z.extractall(input_dir)
            else:
                filename = url.split("/")[-1]
                with open(input_dir / filename, "wb") as f:
                    f.write(response.content)

        # -------------------------------------------------
        # DOWNLOAD TEST DATA (AUTO)
        # -------------------------------------------------

        def download_test_data(url, input_dir):

            response = requests.get(url)

            if response.status_code != 200:
                raise Exception("Download failed")

            if url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                    z.extractall(input_dir)
            else:
                filename = url.split("/")[-1]
                with open(input_dir / filename, "wb") as f:
                    f.write(response.content)

        test_url = metadata.get("test_data_url")

        if not test_url:
            st.warning("⚠️ No test_data_url in metadata")

        else:
            try:
                if not any(input_dir.iterdir()):
                    with st.spinner("📥 Downloading test data..."):
                        download_test_data(test_url, input_dir)

                    st.success("✅ Test data downloaded")

                else:
                    st.info("📂 Test data already available")

            except Exception as e:
                st.error(f"❌ Download error: {e}")

        # -------------------------------------------------
        # RUN TEST
        # -------------------------------------------------

        def normalize_paths(command, project):
            
            command = command.replace(
                "/data/test/data/", f"/data/{project}/test_data/data/"
            )
            
            command = command.replace(
                "/data/test/results/", f"/data/{project}/test_data/results/"
            )

            return command


        def adapt_docker_invocation(invocation):
            
            container_name = socket.gethostname()

            return re.sub(
                r"-v\s+\S+:/data", f"--volumes-from {container_name}", invocation
            )

        if st.button("🚀 Run Test"):
            if not run_command:
                st.error("Provide a command")
                st.stop()

            cmd = adapt_docker_invocation(run_command)
            cmd = normalize_paths(cmd, project)

            progress_bar = st.progress(0)
            status_text = st.empty()

            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            progress = 0
            log_lines = []

            for line in iter(process.stdout.readline, ""):
                log_lines.append(line)
                progress = min(progress + 1, 100)
                progress_bar.progress(progress)
                status_text.text(line.strip())

            process.wait()

            full_log = "\n".join(log_lines)

            if process.returncode == 0:
                st.success("✅ Test completed!")
                st.session_state.test_success = True
                st.session_state.projects_db[project]["test_success"] = True

                if "manual_status" in st.session_state.projects_db[project]:
                    current_status_list = st.session_state.projects_db[project][
                        "manual_status"
                    ]

                    for row in current_status_list:
                        if row["Step"] == "Test":
                            row["Status"] = "DONE ✅"
                            row["Date"] = datetime.now().strftime(
                                "%Y-%m-%d %H:%M"
                            )

                    done_count = sum(
                        1
                        for row in current_status_list
                        if row["Status"] == "DONE ✅"
                    )
                    st.session_state.projects_db[project]["status"] = (
                        f"{done_count}/{len(current_status_list)} Done"
                    )
                    
                save_local_db()

            else:
                st.error("❌ Test failed")
                with st.expander("📄 Test Log"):
                    st.code(full_log)

        # -------------------------------------------------
        # NAVIGATION
        # -------------------------------------------------

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Ontology", "Ontology")

        with col3:
            nav_button("Continue to Status →", "Status")

        st.stop()

    # --- FROM IMAGE (NO DOCKERFILE) ---
    if ptype == "from_image_without_df":
        st.warning(
            "⚠️ **No Build/Test Available for this Project Type**\n\n"
            "This project uses an existing Docker image and does not include a Dockerfile.\n\n"
        )
        
        st.subheader("Test Instructions")

        test_instructions_key = f"test_instructions_{project}"
        if test_instructions_key not in st.session_state:
            st.session_state[test_instructions_key] = p_data.get("test_instructions", "")

        instructions = st.text_area(
            "Describe how to test this Docker image",
            key=test_instructions_key,
            height=200,
            placeholder="Explain how to run and validate the image...",
        )

        apply_primary_button_style()

        saved = p_data.get("test_instructions", "")
        is_dirty = mark_dirty("test_instructions", instructions, saved)

        col1, col2 = st.columns(2)

        # SAVE
        if col1.button("💾 SAVE PROGRESS", use_container_width=True, type="primary", disabled=not is_dirty):
            p_data["test_instructions"] = instructions
            save_local_db()
            set_section_clean("test_instructions")
            st.success("Instructions progress saved!")

        # VALIDATE
        if col2.button("✅ CHECK Instructions", use_container_width=True):
            if not instructions.strip():
                st.error("❌ Instructions cannot be empty")
                st.stop()

            st.success("✅ Instructions valid!")

            project_dir = Path(BASE_PATH) / project
            project_dir.mkdir(parents=True, exist_ok=True)

            instructions_path = project_dir / "instructions.txt"

            with open(instructions_path, "w", encoding="utf-8") as f:
                f.write(instructions)

            st.success(f"Saved to {instructions_path}")
            st.session_state.test_success = True
            st.session_state.projects_db[project]["test_success"] = True

            if "manual_status" in st.session_state.projects_db[project]:
                current_status_list = st.session_state.projects_db[project][
                    "manual_status"
                ]

                for row in current_status_list:
                    if row["Step"] == "Test":
                        row["Status"] = "DONE ✅"
                        row["Date"] = datetime.now().strftime(
                            "%Y-%m-%d %H:%M"
                        )

                done_count = sum(
                    1
                    for row in current_status_list
                    if row["Status"] == "DONE ✅"
                )
                st.session_state.projects_db[project]["status"] = (
                    f"{done_count}/{len(current_status_list)} Done"
                )
            
            save_local_db()
            set_section_clean("test_instructions")

        col1, col2, col3 = st.columns([1, 4, 1])

        with col1:
            nav_button("← Go to Ontology", "Ontology")

        with col3:
            nav_button("Continue to Status →", "Status")

        st.stop()

    # -------------------------------------------------
    # DIRECTORIES
    # -------------------------------------------------

    project_dir = Path(BASE_PATH) / project
    data_dir = project_dir / "test_data"

    input_dir = data_dir / "data"
    output_dir = data_dir / "results"

    project_dir.mkdir(exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------
    # BUILD IMAGE
    # -------------------------------------------------

    st.subheader("Build Image")

    st.warning("""
    ⚠️ Make sure all required files are present in the project folder.
    Docker can only access files inside this directory during build.
    """)

    if st.button("🚀 Build Docker Image"):
        dockerfile_path = project_dir / "Dockerfile"

        if not dockerfile_path.exists():
            st.error("Dockerfile not found in project folder.")
            st.stop()

        st.info("Starting Docker build...")

        success, log_lines = build_docker_image(project, project_dir)

        if success:
            st.success("✅ Docker image built successfully!")
            st.session_state.projects_db[project]["build_success"] = True
            
            if "manual_status" in st.session_state.projects_db[project]:
                current_status_list = st.session_state.projects_db[project][
                    "manual_status"
                ]

                for row in current_status_list:
                    if row["Step"] == "Build":
                        row["Status"] = "DONE ✅"
                        row["Date"] = datetime.now().strftime(
                            "%Y-%m-%d %H:%M"
                        )

                done_count = sum(
                    1
                    for row in current_status_list
                    if row["Status"] == "DONE ✅"
                )
                st.session_state.projects_db[project]["status"] = (
                    f"{done_count}/{len(current_status_list)} Done"
                )
            
            save_local_db()
            
        else:
            st.error("❌ Build failed")
            st.code("\n".join(log_lines[-20:]))

    st.divider()

    # -------------------------------------------------
    # INPUT FILES
    # -------------------------------------------------

    st.subheader("Test Image")
    st.write("##### Available Input Files")

    files = list(input_dir.iterdir())

    if files:
        for f in files:
            st.write("📄", f.name)
    else:
        st.info("No input files.")

    # -------------------------------------------------
    # BUILD COMMAND FROM METADATA
    # -------------------------------------------------

    def build_command_from_metadata(metadata, project):

        general = metadata.get("invocation_general", "")
        test_specific = metadata.get("test_invocation_specific", "")

        if not general:
            return ""

        base_cmd = f"{general} {test_specific}".strip()

        return base_cmd

    # -------------------------------------------------
    # NORMALIZE PATHS
    # -------------------------------------------------

    def normalize_paths(command, project):

        command = command.replace(
            "/data/test/data/", f"/data/{project}/test_data/data/"
        )

        command = command.replace(
            "/data/test/results/", f"/data/{project}/test_data/results/"
        )

        return command

    def adapt_docker_invocation(invocation):

        container_name = socket.gethostname()

        invocation = re.sub(
            r"-v\s+\S+:/data", f"--volumes-from {container_name}", invocation
        )

        return invocation

    # -------------------------------------------------
    # COMMAND UI
    # -------------------------------------------------

    st.write("")
    st.write("##### Execution Command")

    if st.button("🔎 Load command from metadata"):
        metadata = p_data.get("metadata_json", {})

        cmd = build_command_from_metadata(metadata, project)

        if cmd:
            st.session_state.run_command = cmd
            st.success("Command generated from metadata")
        else:
            st.warning("Missing invocation_general in metadata")

    st.caption(
        f"Inputs → /data/{project}/test_data/data | Outputs → /data/{project}/test_data/results"
    )

    run_command = st.text_area(
        "Docker Command", height=120, value=st.session_state.get("run_command", "")
    )

    # -------------------------------------------------
    # RUN TEST
    # -------------------------------------------------

    if st.button("🚀 Run Test"):
        if not run_command:
            st.error("Provide a command.")
            st.stop()

        cmd = adapt_docker_invocation(run_command)
        cmd = normalize_paths(cmd, project)

        # st.code(cmd)

        progress_bar = st.progress(0)
        status_text = st.empty()

        log_lines = []

        with st.spinner("Running container..."):
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            progress = 0

            for line in iter(process.stdout.readline, ""):
                log_lines.append(line.rstrip())

                # progresso simples
                progress = min(progress + 1, 100)
                progress_bar.progress(progress)

                # mostrar última linha (tipo "live status")
                status_text.text(line.strip())

            process.wait()

        full_log = "\n".join(log_lines)

        st.subheader("Program Output")

        if process.returncode == 0:
            progress_bar.progress(100)
            st.success("✅ Test completed!")
            st.session_state.test_success = True
            st.session_state.projects_db[project]["test_success"] = True
            
            if "manual_status" in st.session_state.projects_db[project]:
                current_status_list = st.session_state.projects_db[project][
                    "manual_status"
                ]

                for row in current_status_list:
                    if row["Step"] == "Test":
                        row["Status"] = "DONE ✅"
                        row["Date"] = datetime.now().strftime(
                            "%Y-%m-%d %H:%M"
                        )

                done_count = sum(
                    1
                    for row in current_status_list
                    if row["Status"] == "DONE ✅"
                )
                st.session_state.projects_db[project]["status"] = (
                    f"{done_count}/{len(current_status_list)} Done"
                )
            
            save_local_db()

        else:
            st.error("❌ Test failed")
            with st.expander("📄 Test Image Log"):
                st.code(full_log)

    # -------------------------------------------------
    # BACK
    # -------------------------------------------------
    col1, col2, col3 = st.columns([1, 4, 1])

    with col1:
        nav_button("← Go to Ontology", "Ontology")

    with col3:
        nav_button("Continue to Status →", "Status")


# --- STATUS PAGE ---
elif st.session_state.current_page == "Status":
    sidebar_nave()

    project = st.session_state.active_project
    if not project:
        st.warning("Select a project first!")
        st.stop()

    st.header(f"🚦 Status Dashboard: {project}")

    p_data = st.session_state.projects_db[project]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ----------------------------
    # PROJECT TYPE BADGE
    # ----------------------------
    ptype = p_data.get("project_type", "regular")
    label = format_type_label(ptype)
    color = get_type_color(ptype)

    st.markdown(
        f"""
        <span style="
            background:{color};
            color:white;
            padding:5px 12px;
            border-radius:999px;
            font-size:12px;
            font-weight:600;
        ">
        {label}
        </span>
        """,
        unsafe_allow_html=True,
    )

    # ----------------------------
    # AUTO STATUS FLAGS
    # ----------------------------
    auto_status = {
        "Dockerfile": "DONE ✅"
        if p_data.get("dockerfile") and len(p_data["dockerfile"]) > 10
        else "NOT DONE ❌",

        "README": "DONE ✅"
        if p_data.get("readme") and len(p_data["readme"]) > 10
        else "NOT DONE ❌",

        "Ontology": "DONE ✅"
        if p_data.get("ontology_terms")
        else "NOT DONE ❌",

        "Metadata": "DONE ✅"
        if p_data.get("meta_responses")
        else "NOT DONE ❌",

        "Build": "DONE ✅"
        if p_data.get("build_success")
        else "NOT DONE ❌",

        "Test": "DONE ✅"
        if p_data.get("test_success")
        else "NOT DONE ❌",
    }

    # adapt by project type
    if ptype == "from_image_without_df":
        auto_status.pop("Dockerfile", None)
        auto_status.pop("Build", None)

    if ptype == "update":
        auto_status.pop("Metadata", None)
        auto_status.pop("Ontology", None)

    # ----------------------------
    # INIT / SYNC MANUAL STATUS
    # ----------------------------
    manual = p_data.get("manual_status", [])

    if not manual:
        manual = [
            {"Step": step, "Status": stat, "Date": now_str}
            for step, stat in auto_status.items()
        ]
    else:
        # keep only valid steps
        manual = [row for row in manual if row["Step"] in auto_status]

        existing = {row["Step"] for row in manual}

        # add new steps
        for step, stat in auto_status.items():
            if step not in existing:
                manual.append({"Step": step, "Status": stat, "Date": now_str})

        # auto upgrade status
        for row in manual:
            step = row["Step"]
            auto = auto_status.get(step)

            if auto == "DONE ✅" and row["Status"] != "DONE ✅":
                row["Status"] = "IN PROGRESS ⏳"
                row["Date"] = now_str

    # save synchronized status
    st.session_state.projects_db[project]["manual_status"] = manual

    # ----------------------------
    # TABLE STATUS EDITOR
    # ----------------------------
    status_df = pd.DataFrame(manual)

    edited_df = st.data_editor(
        status_df,
        column_config={
            "Status": st.column_config.SelectboxColumn(
                "Status",
                options=["DONE ✅", "NOT DONE ❌", "IN PROGRESS ⏳", "REVIEW 🔍"],
                required=True,
            ),
            "Date": st.column_config.TextColumn(
                "Last Modification", disabled=True
            ),
        },
        disabled=["Step"],
        use_container_width=True,
        hide_index=True,
    )

    # ----------------------------
    # PROGRESS
    # ----------------------------
    done_count = (edited_df["Status"] == "DONE ✅").sum()
    total_count = len(edited_df)

    progress_percent = int((done_count / total_count) * 100) if total_count else 0

    st.session_state.projects_db[project]["status"] = (
        f"{done_count}/{total_count} Done"
    )

    st.progress(progress_percent)
    st.caption(f"{done_count}/{total_count} steps completed")

    # ----------------------------
    # BUTTONS STYLE
    # ----------------------------
    st.markdown(
        """
        <style>
        button[kind="primary"] {
            background-color: #059669 !important;
            color: white !important;
            border: none !important;
        }
        button[kind="primary"]:hover {
            background-color: #047857 !important;
        }
        button[kind="primary"]:active {
            background-color: #065f46 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)

    # ----------------------------
    # SAVE
    # ----------------------------
    if col1.button(
        "💾 Confirm Status Changes",
        type="primary",
        use_container_width=True,
    ):
        st.session_state.projects_db[project]["manual_status"] = edited_df.to_dict(
            "records"
        )

        save_local_db()
        st.success("Status updated and saved!")

    # ----------------------------
    # SUBMISSION LOGIC
    # ----------------------------
    all_done = total_count > 0 and done_count == total_count

    if all_done:
        st.success("All steps completed! Ready for submission.")
    else:
        st.info("Complete all steps to enable submission package generation.")

    if col2.button(
        "📦 Generate Submission Package",
        disabled=not all_done,
        use_container_width=True,
    ):
        try:
            submission_dir = prepare_submission_folder(project)
            st.success(f"Created at {submission_dir}")
        except Exception as e:
            st.error(f"Error: {e}")

    st.divider()

    # ----------------------------
    # NAVIGATION
    # ----------------------------
    nav_button("← Go to Current Project", "Current Project")


# --- TEST DOCKER IMAGE (FROM FOR_SUBMISSION) ---
elif st.session_state.current_page == "Test Docker Image":
    col1, col2 = st.columns([7,1])
    
    with col1:
        st.header("⚙️ Build & Test (Submission Package)")
    
    with col2:
        st.markdown("<div style='height: 12px'></div>", unsafe_allow_html=True)
        # Button Back to Home
        nav_button("← Back to Home", "Home")

    # -------------------------------------------------
    # PROJECT SELECTION
    # -------------------------------------------------

    projects_root = Path(BASE_PATH)

    available_projects = [
        p.name
        for p in projects_root.iterdir()
        if p.is_dir() and (p / "for_submission").exists()
    ]

    selected_project = st.selectbox(
        "Select project", ["-- Select --"] + available_projects
    )

    manual_project = st.text_input("Or type project name manually")

    project = manual_project.strip() if manual_project else selected_project

    if not project or project == "-- Select --":
        st.warning("Please select or enter a project")
        st.stop()

    submission_dir = Path(BASE_PATH) / project / "for_submission"

    if not submission_dir.exists():
        st.error(f"Project '{project}' does not contain a for_submission folder")
        st.stop()

    st.session_state.active_project = project

    st.success(f"Using project: {project}")

    # -------------------------------------------------
    # PATHS
    # -------------------------------------------------

    dockerfile_path = submission_dir / "Dockerfile"
    metadata_path = submission_dir / "metadata.json"

    input_dir = submission_dir / "test_data" / "input_test_data"
    output_dir = submission_dir / "test_data" / "output_test_data"

    missing_items = []

    if not dockerfile_path.exists():
        missing_items.append("Dockerfile")
    
    # -------------------------------------------------
    # LOAD METADATA
    # -------------------------------------------------

    metadata = None

    if metadata_path.exists():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            st.success("Loaded metadata from submission folder")
        except Exception as e:
            st.warning(f"Failed to read local metadata: {e}")
            st.stop()

    def split_project_name(project_name):
        """
        Ex: clustalomega-2.5.0 → ("clustalomega", "2.5.0")
        """
        match = re.match(r"(.+)-(\d+\.\d+.*)", project_name)

        if match:
            base = match.group(1)
            version = match.group(2)
            return base, version

        return project_name, None

    if not metadata:
        st.warning("No metadata from submission folder")
        st.info("Fetching metadata from GitHub...")

        try:
            base, version = split_project_name(project)

            st.caption(f"Detected base: {base} | version: {version}")

            metadata = get_project_metadata(base)

            if metadata:
                st.success("Metadata loaded from GitHub")
            else:
                st.warning("Failed to fetch metadata from GitHub")
                missing_items.append("metadata (local or GitHub)")

        except Exception as e:
            st.error(f"Error fetching metadata: {e}")
            st.stop()
    
    if missing_items:
        st.error(
            "❌ **Cannot Build and Test Docker Image**\n\n"
            "This feature requires the following:\n\n"
            "- Dockerfile in the for_submission folder\n"
            "- metadata.json (locally or available on GitHub)\n\n"
            f"Missing: {', '.join(missing_items)}\n\n"
            "Please ensure all required files are available before continuing."
        )
        
        st.warning("⚠️ Projects of type **'From Image (without Dockerfile)'** do not support build or test.")
        
        st.stop()

    # -------------------------------------------------
    # BUILD IMAGE
    # -------------------------------------------------

    st.subheader("Build Image")

    if st.button("🚀 Build Docker Image"):
        if not dockerfile_path.exists():
            st.error("Dockerfile not found.")
            st.stop()

        build_cmd = [
            "docker",
            "build",
            "-t",
            f"{project.lower()}_submission",
            str(submission_dir),
        ]

        st.info("Starting Docker build...")

        progress_bar = st.progress(0)
        status_text = st.empty()

        log_lines = []

        process = subprocess.Popen(
            build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        progress = 0

        while True:
            line = process.stdout.readline()

            if not line:
                break

            log_lines.append(line)

            progress = min(progress + 1, 95)
            progress_bar.progress(progress)

            status_text.text(line.strip())

        process.wait()
        progress_bar.progress(100)

        full_log = "".join(log_lines)

        with st.expander("📄 Build Log"):
            st.code(full_log)

        if process.returncode == 0:
            st.success("✅ Docker image built successfully!")
        else:
            st.error("❌ Build failed")
            st.code("\n".join(log_lines[-20:]))

    st.divider()

    # -------------------------------------------------
    # INPUT FILES
    # -------------------------------------------------

    st.subheader("Available Input Files")

    if input_dir.exists():
        files = list(input_dir.iterdir())

        if files:
            for f in files:
                st.write("📄", f.name)
        else:
            st.warning("No input files found")
    else:
        st.warning("Input folder not found")

    # -------------------------------------------------
    # COMMAND LOGIC
    # -------------------------------------------------

    def build_command_from_metadata(metadata):
        general = metadata.get("invocation_general", "")
        specific = metadata.get("test_invocation_specific", "")
        return f"{general}{specific}".strip()

    def normalize_paths(command):
        command = command.replace("/data/test/data/", str(input_dir.resolve()) + "/")
        command = command.replace(
            "/data/test/results/", str(output_dir.resolve()) + "/"
        )
        return command

    def adapt_docker_invocation(invocation):
        container_name = socket.gethostname()

        # substitui volumes -v ...:/data → --volumes-from
        invocation = re.sub(
            r"-v\s+\S+:/data", f"--volumes-from {container_name}", invocation
        )

        return invocation

    # -------------------------------------------------
    # COMMAND UI
    # -------------------------------------------------

    st.subheader("Execution Command")

    if st.button("🔎 Load command from metadata"):
        cmd = build_command_from_metadata(metadata)

        if cmd:
            st.session_state.run_command = cmd
            st.success("Command loaded from metadata.json")
        else:
            st.error("Missing invocation_general")

    run_command = st.text_area(
        "Docker Command", value=st.session_state.get("run_command", ""), height=120
    )

    # -------------------------------------------------
    # RUN TEST
    # -------------------------------------------------

    if st.button("🚀 Run Test"):
        if not run_command:
            st.error("Provide a command.")
            st.stop()

        cmd = adapt_docker_invocation(run_command)
        docker_cmd = normalize_paths(cmd)

        # st.code(docker_cmd)

        progress_bar = st.progress(0)
        status_text = st.empty()

        log_lines = []

        process = subprocess.Popen(
            docker_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        progress = 0

        for line in iter(process.stdout.readline, ""):
            log_lines.append(line.rstrip())

            progress = min(progress + 1, 100)
            progress_bar.progress(progress)

            status_text.text(line.strip())

        process.wait()

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        if process.returncode == 0:
            st.success("✅ Test completed!")

        else:
            st.error("❌ Test failed")

            with st.expander("📄 Logs"):
                st.code("\n".join(log_lines))

else:
    st.title(st.session_state.current_page)
    st.info("Coming soon.")
