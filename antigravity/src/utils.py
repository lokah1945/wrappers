import os
import shutil
import sqlite3
import logging
import fcntl
import contextlib

ORIGINAL_CONFIG_DIR = "/root/.gemini/antigravity-cli"

logger = logging.getLogger("antigravity-wrapper.utils")

def extract_text_content(content) -> str:
    """
    Extracts plain text content from messages. Supports both string content
    and OpenAI-style list of content blocks (e.g. text/image blocks).
    """
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return "\n".join(text_parts)
    return str(content) if content is not None else ""

def format_messages(messages):
    """
    Formats OpenAI/Claude-style messages list into a single structured prompt for agy.
    """
    formatted = []
    system_instruction = ""
    for msg in messages:
        if hasattr(msg, "role"):
            role = msg.role
            content = msg.content
        elif isinstance(msg, dict):
            role = msg.get("role", "user")
            content = msg.get("content", "")
        else:
            role = "user"
            content = str(msg)
            
        text_content = extract_text_content(content)
        
        if role == "system":
            system_instruction = text_content
        elif role == "user":
            formatted.append(f"User: {text_content}")
        elif role == "assistant":
            formatted.append(f"Assistant: {text_content}")
    
    prompt = ""
    if system_instruction:
        prompt += f"System Instructions: {system_instruction}\n\n"
    prompt += "Conversation History:\n" + "\n".join(formatted)
    return prompt

def create_isolated_home(request_id: str) -> str:
    """
    Creates an isolated HOME directory with secure permissions (0o700) to ensure thread safety
    and protect sensitive OAuth token files from other local users.
    """
    tmp_home = f"/tmp/antigravity-home-{request_id}"
    tmp_config_dir = os.path.join(tmp_home, ".gemini/antigravity-cli")
    
    # Securely create the isolated HOME directory with 0o700 (owner access only)
    os.makedirs(tmp_home, mode=0o700, exist_ok=True)
    os.makedirs(tmp_config_dir, mode=0o700, exist_ok=True)
    
    # Ensure directory permissions are strictly 0o700 even if it already existed
    try:
        os.chmod(tmp_home, 0o700)
        os.chmod(tmp_config_dir, 0o700)
    except Exception as e:
        logger.warning(f"Could not enforce strict 0o700 permissions on {tmp_home}: {e}")
    
    # Copy essential configuration files for authentication and settings
    settings_src = os.path.join(ORIGINAL_CONFIG_DIR, "settings.json")
    token_src = os.path.join(ORIGINAL_CONFIG_DIR, "antigravity-oauth-token")
    
    if os.path.exists(settings_src):
        try:
            shutil.copy(settings_src, tmp_config_dir)
        except Exception as e:
            logger.error(f"Failed to copy settings.json to temp home: {e}")
            
    if os.path.exists(token_src):
        try:
            shutil.copy(token_src, tmp_config_dir)
            # Ensure the copied token has secure owner-only read permissions (0o600)
            token_dest = os.path.join(tmp_config_dir, "antigravity-oauth-token")
            os.chmod(token_dest, 0o600)
        except Exception as e:
            logger.error(f"Failed to copy OAuth token to temp home: {e}")
            
    return tmp_home

def cleanup_isolated_home(tmp_home: str):
    """
    Cleans up the temporary isolated HOME directory.
    """
    if os.path.exists(tmp_home) and tmp_home.startswith("/tmp/antigravity-home-"):
        try:
            shutil.rmtree(tmp_home)
        except Exception as e:
            logger.error(f"Error cleaning up isolated home {tmp_home}: {e}")

@contextlib.contextmanager
def conversation_lock(conversation_id: str):
    if not conversation_id:
        yield
        return
    safe_id = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
    if not safe_id:
        yield
        return
    lock_path = f"/tmp/antigravity-db-lock-{safe_id}.lock"
    try:
        f = open(lock_path, "w")
    except Exception as e:
        logger.warning(f"Could not create lock file {lock_path}: {e}")
        yield
        return
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()

def checkpoint_db(db_path: str):
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.close()
        logger.info(f"Successfully checkpointed SQLite database: {db_path}")
    except Exception as e:
        logger.warning(f"Failed to checkpoint database {db_path}: {e}")

def copy_conversation_db(conversation_id: str, tmp_home: str) -> bool:
    """
    Copies the SQLite conversation database from the global configuration directory
    to the isolated temporary HOME directory if it exists, using locking and checkpointing.
    """
    if not conversation_id:
        return False
        
    safe_id = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
    if not safe_id:
        return False
        
    global_conv_db = os.path.join(ORIGINAL_CONFIG_DIR, "conversations", f"{safe_id}.db")
    tmp_conv_dir = os.path.join(tmp_home, ".gemini/antigravity-cli/conversations")
    tmp_conv_db = os.path.join(tmp_conv_dir, f"{safe_id}.db")
    
    with conversation_lock(conversation_id):
        if os.path.exists(global_conv_db):
            # Ensure global DB WAL file is flushed before copying
            checkpoint_db(global_conv_db)
            
            os.makedirs(tmp_conv_dir, mode=0o700, exist_ok=True)
            try:
                # Remove any existing tmp DB or WAL/SHM files to avoid conflicts
                for ext in ["", "-shm", "-wal"]:
                    t_file = tmp_conv_db + ext if ext else tmp_conv_db
                    if os.path.exists(t_file):
                        os.remove(t_file)
                
                shutil.copy(global_conv_db, tmp_conv_db)
                logger.info(f"Successfully copied conversation DB {safe_id} to isolated home.")
                return True
            except Exception as e:
                logger.error(f"Error copying conversation DB to isolated home: {e}")
        return False

def save_conversation_db(conversation_id: str, tmp_home: str) -> bool:
    """
    Copies the updated conversation database from the isolated temporary HOME directory
    back to the global configuration directory, using locking and checkpointing.
    """
    if not conversation_id:
        return False
        
    safe_id = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
    if not safe_id:
        return False
        
    global_conv_dir = os.path.join(ORIGINAL_CONFIG_DIR, "conversations")
    global_conv_db = os.path.join(global_conv_dir, f"{safe_id}.db")
    tmp_conv_db = os.path.join(tmp_home, ".gemini/antigravity-cli/conversations", f"{safe_id}.db")
    
    with conversation_lock(conversation_id):
        if os.path.exists(tmp_conv_db):
            # Checkpoint the isolated DB to merge WAL changes
            checkpoint_db(tmp_conv_db)
            
            os.makedirs(global_conv_dir, mode=0o700, exist_ok=True)
            try:
                # Remove existing shm/wal in global store to start fresh with clean checkpointed DB
                for ext in [".db-shm", ".db-wal"]:
                    g_ext = global_conv_db.replace(".db", ext)
                    if os.path.exists(g_ext):
                        try:
                            os.remove(g_ext)
                        except Exception:
                            pass
                
                shutil.copy(tmp_conv_db, global_conv_db)
                logger.info(f"Successfully saved conversation DB {safe_id} back to global config store.")
                return True
            except Exception as e:
                logger.error(f"Error saving conversation DB back to global store: {e}")
        return False

def map_new_conversation(tmp_home: str, custom_id: str):
    """
    Scans the isolated home's conversations folder. If agy created a new DB file
    with a random UUID, renames it to <custom_id>.db and updates the internal
    cascade_id and trajectory_id to match. Protected by file lock.
    """
    if not custom_id:
        return
        
    safe_id = "".join(c for c in custom_id if c.isalnum() or c in "-_")
    if not safe_id:
        return
        
    conv_dir = os.path.join(tmp_home, ".gemini/antigravity-cli/conversations")
    if not os.path.exists(conv_dir):
        return
        
    # Find any db file that is not the mapped custom_id.db
    db_files = [f for f in os.listdir(conv_dir) if f.endswith(".db") and f != f"{safe_id}.db"]
    if not db_files:
        return
        
    # Take the first newly created database file
    new_db_file = db_files[0]
    src_path = os.path.join(conv_dir, new_db_file)
    dest_path = os.path.join(conv_dir, f"{safe_id}.db")
    
    with conversation_lock(custom_id):
        logger.info(f"Mapping new conversation: renaming {new_db_file} to {safe_id}.db")
        try:
            # Checkpoint the source database first to merge WAL entries
            checkpoint_db(src_path)
            
            # Remove any existing destination DB/shm/wal to prevent rename failure
            for ext in ["", "-shm", "-wal"]:
                d_file = dest_path + ext if ext else dest_path
                if os.path.exists(d_file):
                    os.remove(d_file)
                    
            os.rename(src_path, dest_path)
            # Remove any source shm/wal files since they are checkpointed and truncated
            for ext in [".db-shm", ".db-wal"]:
                src_ext = os.path.join(conv_dir, new_db_file.replace(".db", ext))
                if os.path.exists(src_ext):
                    try:
                        os.remove(src_ext)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Failed to rename newly created conversation DB files: {e}")
            return
            
        # Update internal sqlite metadata to allow agy to recognize the custom_id next time
        try:
            conn = sqlite3.connect(dest_path)
            conn.cursor().execute(
                "UPDATE trajectory_meta SET cascade_id = ?, trajectory_id = ?",
                (safe_id, safe_id)
            )
            conn.commit()
            conn.close()
            logger.info(f"Successfully updated internal database metadata to cascade_id={safe_id}")
        except Exception as e:
            logger.error(f"Failed to update internal sqlite trajectory_meta: {e}")
