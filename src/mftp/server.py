"""MFTP Server - File transfer server for Meshtastic networks."""

import argparse
import base64
import hashlib
import json
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from pubsub import pub

from mftp.common import MeshtasticConnection, select_device_sync

# Track processed packet IDs to avoid duplicates
processed_packets = set()

# Chunk size for file transfer (150 bytes to fit in Meshtastic messages)
CHUNK_SIZE = 150

# Maximum filename length (to fit in message payloads)
MAX_FILENAME_LENGTH = 10

# Maximum number of files to serve (limited by 200-byte DM payload for !ls response)
# With max 10-char filenames and no size field, we can fit 4 files in 200 bytes
MAX_FILE_COUNT = 4

# Storage for chunked files
file_chunks = {}


@dataclass
class FileInfo:
    """Information about a chunked file."""

    name: str
    size: int  # Original file size in bytes
    chunks: list[str]  # Base64 encoded chunks
    md5_hash: str  # MD5 hash of original file

    @property
    def num_chunks(self) -> int:
        return len(self.chunks)


def chunk_file(file_path: Path) -> FileInfo:
    """Read a file, base64 encode it, and chunk it for transmission.

    Args:
        file_path: Path to the file to chunk.

    Returns:
        FileInfo object with chunked data.
    """
    # Read file bytes
    with open(file_path, "rb") as f:
        file_data = f.read()

    # Calculate MD5 hash of original file
    md5_hash = hashlib.md5(file_data).hexdigest()

    # Base64 encode the entire file
    encoded_data = base64.b64encode(file_data).decode("ascii")

    # Split into chunks
    chunks = [
        encoded_data[i : i + CHUNK_SIZE]
        for i in range(0, len(encoded_data), CHUNK_SIZE)
    ]

    return FileInfo(
        name=file_path.name, size=len(file_data), chunks=chunks, md5_hash=md5_hash
    )


def prepare_files(directory: Path) -> dict[str, FileInfo]:
    """Prepare all files in directory by chunking them.

    Args:
        directory: Directory containing files to serve.

    Returns:
        Dictionary mapping filename to FileInfo.
    """
    chunked_files = {}

    logger.info(f"Preparing files from {directory}")

    # First pass: collect valid files
    valid_files = []
    for item in directory.iterdir():
        if item.is_file():
            # Check filename length
            if len(item.name) > MAX_FILENAME_LENGTH:
                logger.warning(
                    f"Skipping: {item.name} (filename too long, max {MAX_FILENAME_LENGTH} chars)"
                )
                continue
            valid_files.append(item)

    # Check if we exceed the maximum file count
    if len(valid_files) > MAX_FILE_COUNT:
        logger.warning(
            f"Found {len(valid_files)} files, but can only serve {MAX_FILE_COUNT} files"
        )
        logger.warning(f"Limited by 200-byte DM payload size for !ls response")
        logger.info(f"Serving only the first {MAX_FILE_COUNT} files")
        valid_files = valid_files[:MAX_FILE_COUNT]

    # Second pass: chunk the files
    for item in valid_files:
        try:
            file_info = chunk_file(item)
            chunked_files[item.name] = file_info
            logger.success(f"Processed: {item.name} ({file_info.num_chunks} chunks)")
        except Exception as e:
            logger.error(f"Error processing {item.name}: {e}")

    logger.success(f"Prepared {len(chunked_files)} file(s)")
    return chunked_files


def list_files_json(file_chunks: dict[str, FileInfo]) -> str:
    """List available files in JSON format for client parsing.

    Args:
        file_chunks: Dictionary of prepared files.

    Returns:
        JSON string with file information.
    """
    files = []
    for filename in sorted(file_chunks.keys()):
        file_info = file_chunks[filename]
        files.append({"name": filename, "chunks": file_info.num_chunks})

    return json.dumps({"files": files})


def on_receive(packet, interface, file_chunks: dict[str, FileInfo]):
    """Handle received messages.

    Args:
        packet: The received packet.
        interface: The Meshtastic interface.
        file_chunks: Dictionary of prepared files.
    """
    try:
        # Deduplicate packets - mesh can rebroadcast DMs and we see them multiple times
        packet_id = packet.get("id")
        if packet_id in processed_packets:
            return  # Already processed this packet
        processed_packets.add(packet_id)

        # Limit the size of the processed_packets set to avoid memory issues
        # Remove oldest half when we hit the limit
        if len(processed_packets) > 1000:
            # Convert to list, remove first half, convert back to set
            packet_list = list(processed_packets)
            processed_packets.clear()
            processed_packets.update(packet_list[500:])

        # Check if this is a text message
        if "decoded" in packet:
            decoded = packet["decoded"]

            # Check for text payload
            if "text" in decoded:
                text = decoded["text"]
                from_id = packet.get("fromId", "unknown")
                to_id = packet.get("toId", "unknown")

                # Get our node ID to check if this is a DM to us
                my_node_id = interface.myInfo.my_node_num
                my_node_id_hex = f"!{my_node_id:08x}"

                # Check if this is a direct message to us (not broadcast)
                is_dm = to_id == my_node_id_hex or packet.get("to") == my_node_id

                # Only process DMs to this node
                if is_dm:
                    logger.info(f"DM from {from_id}: '{text}'")

                    # Check if message starts with command prefix
                    text_stripped = text.strip()
                    if text_stripped.startswith("!"):
                        parts = text_stripped.split(maxsplit=2)
                        command = parts[0]

                        # Handle !ls command (JSON format for client)
                        if command == "!ls":
                            logger.info(f"Processing !ls command")
                            response = list_files_json(file_chunks)
                            interface.sendText(response, destinationId=from_id)
                            logger.success(f"Sent file list to {from_id}")

                        # Handle !req <filename> <chunk_num> command
                        elif command == "!req" and len(parts) == 3:
                            filename = parts[1]
                            try:
                                chunk_num = int(parts[2])
                                logger.info(f"Request for {filename} chunk {chunk_num}")

                                # Check if file exists
                                if filename not in file_chunks:
                                    error_msg = f"!error File not found: {filename}"
                                    interface.sendText(error_msg, destinationId=from_id)
                                    logger.error(f"File not found: {filename}")
                                    return

                                file_info = file_chunks[filename]

                                # Check if chunk number is valid
                                if chunk_num < 0 or chunk_num >= file_info.num_chunks:
                                    error_msg = f"!error Invalid chunk {chunk_num} for {filename}"
                                    interface.sendText(error_msg, destinationId=from_id)
                                    logger.error(f"Invalid chunk number: {chunk_num}")
                                    return

                                # Send the requested chunk
                                chunk_data = file_info.chunks[chunk_num]
                                response = f"!chunk {filename} {chunk_num} {chunk_data}"
                                interface.sendText(response, destinationId=from_id)
                                logger.success(
                                    f"Sent chunk {chunk_num}/{file_info.num_chunks-1} to {from_id}"
                                )

                            except ValueError:
                                error_msg = "!error Invalid chunk number format"
                                interface.sendText(error_msg, destinationId=from_id)
                                logger.error(f"Invalid chunk number format")

                        # Handle !check <filename> command
                        elif command == "!check" and len(parts) == 2:
                            filename = parts[1]
                            logger.info(f"Checksum request for {filename}")

                            # Check if file exists
                            if filename not in file_chunks:
                                error_msg = f"!error File not found: {filename}"
                                interface.sendText(error_msg, destinationId=from_id)
                                logger.error(f"File not found: {filename}")
                                return

                            file_info = file_chunks[filename]

                            # Send checksum response
                            response = f"!checksum {filename} {file_info.md5_hash}"
                            interface.sendText(response, destinationId=from_id)
                            logger.success(f"Sent checksum for {filename} to {from_id}")

                        else:
                            # Unknown command
                            logger.warning(f"Unknown command: '{text_stripped}'")
                            interface.sendText(
                                f"Unknown command: {text_stripped}\nAvailable: !ls, !req <filename> <chunk>, !check <filename>",
                                destinationId=from_id,
                            )

    except Exception as e:
        logger.exception(f"Error handling message: {e}")


def main():
    """Main entry point for MFTP server."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="MFTP Server - Meshtastic File Transfer Protocol Server"
    )
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        required=True,
        help="Directory to serve files from",
    )
    args = parser.parse_args()

    # Validate directory exists
    serve_dir = Path(args.directory).resolve()
    if not serve_dir.exists():
        logger.error(f"Directory '{serve_dir}' does not exist")
        sys.exit(1)

    if not serve_dir.is_dir():
        logger.error(f"'{serve_dir}' is not a directory")
        sys.exit(1)

    logger.info("MFTP Server - Meshtastic File Transfer Protocol")
    logger.info(f"Serving files from: {serve_dir}")

    # Prepare files by chunking them
    chunked_files = prepare_files(serve_dir)

    # Select device using TUI
    device_info = select_device_sync()

    if not device_info:
        logger.warning("No device selected. Exiting.")
        sys.exit(0)

    logger.info(f"Connecting to {device_info}")

    # Connect to the selected device
    with MeshtasticConnection() as mesh:
        if not mesh.connect(device_info):
            logger.error("Failed to connect to device")
            sys.exit(1)

        logger.success("Connected to device successfully!")

        # Display server node ID
        my_node_id = mesh.interface.myInfo.my_node_num
        my_node_id_hex = f"{my_node_id:08x}"
        logger.info(f"Server Node ID: !{my_node_id_hex}")
        logger.info(f"Connect a client with: uv run mftp-client -s {my_node_id_hex}")

        logger.info("MFTP Server is listening for file requests")
        logger.info("Commands: !ls, !req <filename> <chunk>, !check <filename>")
        logger.info("Press Ctrl+C to exit")

        # Define callback that has access to chunked files
        def message_handler(packet, interface):
            on_receive(packet, interface, chunked_files)

        # Subscribe to receive text messages
        # Note: Only subscribing to .receive.text, not .receive to avoid duplicates
        pub.subscribe(message_handler, "meshtastic.receive.text")

        logger.success("Subscribed to message events - waiting for messages...")

        # Keep server running
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            logger.info("Shutting down server...")
            pub.unsubscribe(message_handler, "meshtastic.receive.text")


if __name__ == "__main__":
    main()
