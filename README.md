[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/evilran-baidu-mcp-server-badge.jpg)](https://mseep.ai/app/evilran-baidu-mcp-server)

# Baidu Search MCP Server

[![smithery badge](https://smithery.ai/badge/@Evilran/baidu-mcp-server)](https://smithery.ai/server/@Evilran/baidu-mcp-server)

A Model Context Protocol (MCP) server that provides web search capabilities through Baidu, with additional features for content fetching and parsing.

<a href="https://glama.ai/mcp/servers/@Evilran/baidu-mcp-server">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@Evilran/baidu-mcp-server/badge" alt="Baidu Server MCP server" />
</a>

## Features

- **Web Search**: Search Baidu with advanced rate limiting and result formatting
- **Content Fetching**: Retrieve and parse webpage content with intelligent text extraction
- **Rate Limiting**: Built-in protection against rate limits for both search and content fetching
- **Error Handling**: Comprehensive error handling and logging
- **LLM-Friendly Output**: Results formatted specifically for large language model consumption

## Installation

### Installing via Smithery

To install Baidu Search Server for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@Evilran/baidu-mcp-server):

```bash
npx -y @smithery/cli install @Evilran/baidu-mcp-server --client claude
```

### Installing via `uv`

Install directly from PyPI using `uv`:

```bash
uv pip install baidu-mcp-server
```

## Usage

### Running with Claude Desktop

1. Download [Claude Desktop](https://claude.ai/download)
2. Create or edit your Claude Desktop configuration:
   - On macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - On Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the following configuration:

```json
{
    "mcpServers": {
        "baidu-search": {
            "command": "uvx",
            "args": ["baidu-mcp-server"]
        }
    }
}
```

3. Restart Claude Desktop

### Development

For local development, you can use the MCP CLI:

```bash
# Run with the MCP Inspector
mcp dev server.py

# Install locally for testing with Claude Desktop
mcp install server.py
```
## Available Tools

### 1. Search Tool

```python
async def search(query: str, max_results: int = 10) -> str
```

Performs a web search on Baidu and returns formatted results.

**Parameters:**
- `query`: Search query string
- `max_results`: Maximum number of results to return (default: 10)

**Returns:**
Formatted string containing search results with titles, URLs, and snippets.

### 2. Content Fetching Tool

```python
async def fetch_content(url: str) -> str
```

Fetches and parses content from a webpage.

**Parameters:**
- `url`: The webpage URL to fetch content from

**Returns:**
Cleaned and formatted text content from the webpage.

## Features in Detail

### Rate Limiting

- Search: Limited to 30 requests per minute
- Content Fetching: Limited to 20 requests per minute
- Automatic queue management and wait times

### Result Processing

- Removes ads and irrelevant content
- Cleans up Baidu redirect URLs
- Formats results for optimal LLM consumption
- Truncates long content appropriately

### Error Handling

- Comprehensive error catching and reporting
- Detailed logging through MCP context
- Graceful degradation on rate limits or timeouts

## Contributing

Issues and pull requests are welcome! Some areas for potential improvement:

- Additional search parameters (region, language, etc.)
- Enhanced content parsing options
- Caching layer for frequently accessed content
- Additional rate limiting strategies

## License

This project is licensed under the MIT License.

## Acknowledgments

The code in this project references the following repositories:

- [nickclyde/duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server)
- [BaiduSpider/BaiduSpider](https://github.com/BaiduSpider/BaiduSpider)

Thanks to the authors and contributors of these repositories for their efforts and contributions to the open-source community.