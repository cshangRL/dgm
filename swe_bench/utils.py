import tarfile
import io
import logging
import shlex
import threading
from typing import Union, Optional
from pathlib import Path
import docker

from utils.security_utils import (
    MAX_ARCHIVE_BYTES,
    MAX_LOG_BYTES,
    safe_extract_single_file,
    safe_extract_tar,
    truncate_bytes,
)

# Thread-local storage for loggers
_thread_local = threading.local()

def get_thread_logger():
    """Get the logger instance specific to the current thread."""
    return getattr(_thread_local, 'logger', None)

def setup_logger(log_file):
    """
    Set up a thread-safe logger with file handler.
    
    Args:
        log_file (str): Path to the log file
        
    Returns:
        logging.Logger: Thread-specific logger instance
    """
    # Create logger with thread-specific name
    thread_id = threading.get_ident()
    logger_name = f'docker_logger_{thread_id}'
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    
    # Clear existing handlers
    for handler in logger.handlers:
        logger.removeHandler(handler)

    # Create file handler with lock
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.INFO)
    handler.stream.lock = threading.Lock()
    
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    
    # Add handler to logger
    logger.addHandler(handler)
    
    # Store logger in thread local storage
    _thread_local.logger = logger
    
    return logger

def safe_log(message: str, level: int = logging.INFO):
    """Thread-safe logging function."""
    logger = get_thread_logger()
    if logger:
        logger.log(level, message)
    else:
        print(f"Warning: No logger found for thread {threading.get_ident()}")

def remove_existing_container(client, container_name):
    """
    Check if a container with the specified name exists, and remove it if so.
    """
    try:
        existing_container = client.containers.get(container_name)
        safe_log(f"Removing existing container with name {container_name}")
        existing_container.stop()
        existing_container.remove()
    except docker.errors.NotFound:
        # Container does not exist, no action needed
        safe_log(f"No existing container with name {container_name} found.")
    except docker.errors.APIError as e:
        safe_log(f"Error removing existing container {container_name}: {e}", logging.ERROR)
        raise

def create_archive(path: Union[str, Path], data: Optional[bytes] = None) -> bytes:
    """
    Create a tar archive containing either file data or a directory structure.
    
    Args:
        path (Union[str, Path]): Path where the file/directory should be placed in the container
        data (Optional[bytes]): File content in bytes if creating archive for a single file
    
    Returns:
        bytes: Tar archive containing the file or directory structure
    """
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        if data is not None:
            # Handle single file
            tarinfo = tarfile.TarInfo(name=str(path))
            tarinfo.size = len(data)
            tar.addfile(tarinfo, io.BytesIO(data))
        else:
            # Handle directory
            path = Path(path)
            arcname = path.name
            tar.add(path, arcname=arcname)
    
    tar_stream.seek(0)
    return tar_stream.read()

def copy_to_container(container, source_path: Union[str, Path], dest_path: Union[str, Path]) -> None:
    """
    Copy a file or directory from the local system to a Docker container.
    
    Args:
        container: Docker container object
        source_path (Union[str, Path]): Path to the source file/directory on local system
        dest_path (Union[str, Path]): Destination path in the container
    
    Raises:
        FileNotFoundError: If source path doesn't exist
        Exception: For other errors during copy operation
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)
    
    try:
        if not source_path.exists():
            raise FileNotFoundError(f"Source path not found: {source_path}")
            
        # Determine container destination directory
        if source_path.is_file():
            container_dest_dir = str(dest_path.parent)
            archive_path = dest_path.name
            with open(source_path, 'rb') as source_file:
                data = source_file.read()
            archive = create_archive(archive_path, data)
        else:
            # For directories, we want to copy to the parent of dest_path
            container_dest_dir = str(dest_path.parent)
            archive = create_archive(source_path)
            
        # Create destination directory in container if it doesn't exist
        container.exec_run(f"mkdir -p {shlex.quote(container_dest_dir)}")

        safe_log(f"Copying {source_path} to container at {dest_path}")
        success = container.put_archive(container_dest_dir, archive)
        
        if not success:
            raise Exception(f"Failed to copy {source_path} to container")
            
        safe_log(f"Successfully copied {source_path} to container")
        
    except Exception as e:
        safe_log(f"Error copying to container: {e}", logging.ERROR)
        raise

def copy_from_container(container, source_path: Union[str, Path], dest_path: Union[str, Path]) -> None:
    """
    Copy a file or directory from a Docker container to the local system.
    
    Args:
        container: Docker container object
        source_path (Union[str, Path]): Path to the source file/directory in container
        dest_path (Union[str, Path]): Destination path on local system
    
    Raises:
        FileNotFoundError: If source path doesn't exist in container
        Exception: For other errors during copy operation
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)
    
    try:
        # Check if source exists in container. Quote the path: it can contain
        # characters that are meaningful to the container shell.
        quoted_source = shlex.quote(str(source_path))
        result = container.exec_run(f"test -e {quoted_source}")
        if result.exit_code and result.exit_code != 0:
            raise FileNotFoundError(f"Source path not found in container: {source_path}")
            
        # GNU stat: the containers are Debian/Ubuntu. `stat -f` is BSD-only and
        # always failed, forcing every copy through tar.extractall.
        result = container.exec_run(f"stat -c '%F' {quoted_source}")
        file_type = result.output.decode().strip().lower() if result.output else ""
        is_file = file_type.startswith("regular")
            
        # Create destination directory if it doesn't exist
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        safe_log(f"Copying from container {source_path} to local path {dest_path}")
        
        # Get archive from container, capping the number of bytes we buffer.
        bits, stat = container.get_archive(str(source_path))
        chunks = []
        total = 0
        for chunk in bits:
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    f"Container archive exceeds {MAX_ARCHIVE_BYTES} bytes: {source_path}"
                )
            chunks.append(chunk)
        archive_data = b"".join(chunks)
        
        if is_file:
            safe_extract_single_file(
                archive_data,
                dest_path,
                expected_name=Path(str(source_path)).name,
            )
        else:
            safe_extract_tar(archive_data, dest_path.parent)
            extracted_path = dest_path.parent / Path(stat['name']).name
            if extracted_path != dest_path:
                if not extracted_path.exists():
                    raise FileNotFoundError(
                        f"Archive did not contain expected path: {stat['name']}"
                    )
                extracted_path.rename(dest_path)
        
        safe_log(f"Successfully copied from container to {dest_path}")
        
    except Exception as e:
        safe_log(f"Error copying from container: {e}", logging.ERROR)
        raise

def log_container_output(exec_result, raise_error=True):
    """
    Log output from a Docker container execution, handling both streaming and non-streaming cases.
    """
    # Handle output logging
    if isinstance(exec_result.output, bytes):
        # Handle non-streaming output
        safe_log(f"Container output: {truncate_bytes(exec_result.output).decode(errors='replace')}")
    else:
        # Handle streaming output, bounding total bytes logged.
        total = 0
        for chunk in exec_result.output:
            if chunk:
                if total >= MAX_LOG_BYTES:
                    safe_log("Container output: ...[truncated by dgm security limit]...")
                    break
                total += len(chunk)
                safe_log(f"Container output: {truncate_bytes(chunk, MAX_LOG_BYTES).decode(errors='replace').strip()}")
    
    # Check exit code
    if raise_error:
        if exec_result.exit_code and exec_result.exit_code != 0:
            error_msg = f"Script failed with exit code {exec_result.exit_code}"
            safe_log(error_msg, logging.ERROR)
            raise Exception(error_msg)
