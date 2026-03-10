from collections import OrderedDict

from mindsdb.integrations.libs.const import HANDLER_CONNECTION_ARG_TYPE as ARG_TYPE


connection_args = OrderedDict(
    host={
        "type": ARG_TYPE.STR,
        "description": "Base URL of the Prometheus-compatible API (e.g. http://vmdb:8428)",
        "required": True,
        "label": "Host URL",
    },
    timeout={
        "type": ARG_TYPE.INT,
        "description": "HTTP request timeout in seconds",
        "required": False,
        "label": "Timeout (seconds)",
    },
    username={
        "type": ARG_TYPE.STR,
        "description": "Basic auth username (optional)",
        "required": False,
        "label": "Username",
    },
    password={
        "type": ARG_TYPE.PWD,
        "description": "Basic auth password (optional)",
        "required": False,
        "label": "Password",
        "secret": True,
    },
    verify_ssl={
        "type": ARG_TYPE.BOOL,
        "description": "Whether to verify SSL certificates",
        "required": False,
        "label": "Verify SSL",
    },
)

connection_args_example = OrderedDict(
    host="http://vmdb:8428",
    timeout=30,
    username=None,
    password=None,
    verify_ssl=False,
)
