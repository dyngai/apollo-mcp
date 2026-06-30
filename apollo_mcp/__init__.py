"""apollo-mcp: Model Context Protocol server and CLI for Apollo.io.

Built on FastMCP.
"""

from apollo_mcp.apollo import ApolloClient, ApolloError

__version__ = "0.1.0"
__all__ = ["ApolloClient", "ApolloError", "__version__"]
