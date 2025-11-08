# MeshFTP - Meshtastic File Transfer Protocol

A simple file transfer system for Meshtastic mesh networks, enabling file sharing between nodes over long-range radio links.

## Overview

MeshFTP consists of two components:

- **Server**: Serves files from a directory to other mesh nodes
- **Client**: Downloads files from a server node on the mesh network

Files are chunked, base64-encoded, and transferred over Meshtastic direct messages (DMs) with automatic retry logic and MD5 checksum validation.

MeshFTP is designed for small files due to Meshtastic's low bandwidth and DM size limitations. It is ideal for sharing text files, small images, or configuration files in remote areas without internet access. Be kind, remember everyone's bandwidth is limited and transferring large files may impact other users on the mesh.

## Installation

Install using `uv`:

```bash
# Clone the repository
gh repo clone richstokes/MeshFTP
cd MeshFTP

# Install dependencies
uv sync
```

## Usage

### Server

Start a file server to share files with other nodes:

```bash
uv run mftp-server -d /path/to/files
```

**Options:**

- `-d, --directory` (required): Directory containing files to serve

**Example:**

```bash
uv run mftp-server -d ./example_files
```

On startup, the server will:

1. Show available Meshtastic devices (serial and Bluetooth)
2. Let you select a device
3. Process and chunk all files in the specified directory
4. Display its node ID (e.g., `!abcd1234`)
5. Listen for file requests from clients

**Supported Commands:**

- `!ls` - List available files
- `!req <filename> <chunk>` - Request a specific file chunk
- `!check <filename>` - Request MD5 checksum for validation

### Client

Download files from a server node using the interactive TUI client:

```bash
uv run mftp-client -s <server-node-id> [-d <download-directory>]
```

**Options:**

- `-s, --server` (required): Server node ID to connect to (e.g., `!abcd1234` or `abcd1234`)
- `-d, --directory` (optional): Download directory (default: `~/Downloads/MFTP`)

**Example:**

```bash
# Use default download directory
uv run mftp-client -s abcd1234

# Specify custom download directory
uv run mftp-client -s abcd1234 -d ./my-downloads
```

On startup, the client will:

1. Show available Meshtastic devices
2. Let you select a device
3. Connect to the mesh network
4. Enter interactive mode, letting you list and download files from the server


## Protocol Details

### Message Format

MFTP uses Meshtastic direct messages with a simple command protocol:

**Client → Server:**

- `!ls` - Request file list
- `!req <filename> <chunk_number>` - Request specific chunk
- `!check <filename>` - Request file checksum

**Server → Client:**

- `{"files":[{"name":"file.txt","chunks":5},...]}` - JSON file list
- `!chunk <filename> <chunk_number> <base64_data>` - File chunk response
- `!checksum <filename> <md5_hash>` - Checksum response
- `!error <message>` - Error message

### Transfer Process

1. Client sends `!ls` to request file list
2. Server responds with JSON containing available files
3. Client displays files and user selects one
4. Client requests chunks sequentially with 30-second timeout
5. Server sends each chunk as base64-encoded data
6. Client retries failed chunks up to 5 times with exponential backoff
7. After all chunks received, client reassembles and saves file
8. Client requests checksum and validates downloaded file
9. Transfer complete if checksum matches

### Chunking

- Files are split into 150-byte chunks (after base64 encoding)
- Chunk size chosen to fit within Meshtastic DM payload
- Each chunk is transmitted independently with error handling
- Progress displayed as "Chunk X/Y" during download

## Limitations

### File Constraints

- **Maximum filename length**: 10 characters
  - Required to fit file list in Meshtastic DM payload
  - Files with longer names are skipped with a warning
  
- **Maximum files per directory**: 4 files
  - Limited by Meshtastic DM payload for `!ls` response
  - With 10-char filenames, only 4 files fit in JSON list
  - Excess files are ignored with a warning message
  - May implement pagination for larger file lists in future
  - In practical terms, it works best with just a few small files in a dedicated directory

### Network Constraints

- **DM payload size**: ~200 bytes maximum
  - Meshtastic limitation for direct messages
  - Affects chunk size and file list capacity
  
- **Transfer speed**: Very slow
  - Meshtastic has low bitrate (LoRa is long-range, not high-speed)
  - Mesh network adds latency for multi-hop routes
  - Expect minutes for small files, longer for large files

- **Sequential chunks only**:
  - Chunks must be downloaded in order
  - No parallel chunk downloads
  - No chunk priority/reordering

### Reliability

- **Retry logic**: 5 attempts per chunk with 30-second timeout and exponential backoff
- **Checksum validation**: MD5 hash verification after download
- **Packet deduplication**: Handles mesh network rebroadcasts
- **No guaranteed delivery**: Mesh networks are unreliable
  - Obstacles, distance, interference can cause failures
  - Transfer may fail if connection is lost

### Other Limitations

- **No authentication**: Anyone on the mesh can access served files
- **No encryption**: Files transferred in plaintext (base64 encoded). I don't fully understand if/how Meshtastic encrypts DMs so send at your own risk!
- **No compression**: Files sent as-is, no size optimization - compress your files first
- **No directory browsing**: Only serves files from one directory
- **No file deletion**: Server directory is read-only
- **No directory refresh**: Files are loaded on server startup only, restart to update

## Tips

- **Keep filenames short** (10 chars or less, including extension)
- **Serve only a few files** (4 maximum) at a time
- **Use small files** for realistic transfers
