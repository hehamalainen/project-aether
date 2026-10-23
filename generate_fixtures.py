import json
import os

DEFAULT_GATE_CONFIG = {
    "gates": [
        {
            "name": "syntax",
            "command": "python3 -m py_compile *.py",
            "timeout_seconds": 10
        },
        {
            "name": "unit_tests",
            "command": "python3 app.py",
            "timeout_seconds": 10
        }
    ]
}

fixtures_dir = "tests/fixtures/phase4"

fixtures = {
    
    "mixed": {
        "oracle": {
            "scenario": "mixed",
            "expected_outcome": "PROMOTED",
            "expected_reason": "synthesis_success",
            "agents": [
                {
                                "id": "agent_1",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 1')\n"
                                }
                },
                {
                                "id": "agent_2",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 2')\n"
                                }
                },
                {
                                "id": "agent_3",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 3')\n"
                                }
                },
                {
                                "id": "agent_4",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 4')\n"
                                }
                },
                {
                                "id": "agent_5",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 5')\n"
                                }
                },
                {
                                "id": "agent_6",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 6')\n"
                                }
                },
                {
                                "id": "agent_7",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 7')\n"
                                }
                },
                {
                                "id": "agent_8",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 8')\n"
                                }
                },
                {
                                "id": "agent_9",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 9')\n"
                                }
                },
                {
                                "id": "agent_10",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 10')\n"
                                }
                },
                {
                                "id": "agent_11",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 11')\n"
                                }
                },
                {
                                "id": "agent_12",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 12')\n"
                                }
                },
                {
                                "id": "agent_13",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 13')\n"
                                }
                },
                {
                                "id": "agent_14",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 14')\n"
                                }
                },
                {
                                "id": "agent_15",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 15')\n"
                                }
                },
                {
                                "id": "agent_16",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 16')\n"
                                }
                },
                {
                                "id": "agent_17",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 17')\n"
                                }
                },
                {
                                "id": "agent_18",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 18')\n"
                                }
                },
                {
                                "id": "agent_19",
                                "proposal": {
                                                "path": "utils.py",
                                                "affected_symbols": [
                                                                "utils.py::FUNC:helper"
                                                ],
                                                "content": "def helper():\n    print('mixed agent 19')\n"
                                }
                },
                {
                                "id": "agent_20",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('mixed agent 20')\n"
                                }
                }
],
            "golden_assertions": {},
            "gate_config": DEFAULT_GATE_CONFIG
        },
        "base": {
            "app.py": "def start():\n    pass\n",
            "utils.py": "def helper():\n    pass\n"
        }
    },
    "hotspot": {
        "oracle": {
            "scenario": "hotspot",
            "expected_outcome": "PROMOTED",
            "expected_reason": "synthesis_success",
            "agents": [
                {
                                "id": "agent_1",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 1')\n"
                                }
                },
                {
                                "id": "agent_2",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 2')\n"
                                }
                },
                {
                                "id": "agent_3",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 3')\n"
                                }
                },
                {
                                "id": "agent_4",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 4')\n"
                                }
                },
                {
                                "id": "agent_5",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 5')\n"
                                }
                },
                {
                                "id": "agent_6",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 6')\n"
                                }
                },
                {
                                "id": "agent_7",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 7')\n"
                                }
                },
                {
                                "id": "agent_8",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 8')\n"
                                }
                },
                {
                                "id": "agent_9",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 9')\n"
                                }
                },
                {
                                "id": "agent_10",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 10')\n"
                                }
                },
                {
                                "id": "agent_11",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 11')\n"
                                }
                },
                {
                                "id": "agent_12",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 12')\n"
                                }
                },
                {
                                "id": "agent_13",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 13')\n"
                                }
                },
                {
                                "id": "agent_14",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 14')\n"
                                }
                },
                {
                                "id": "agent_15",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 15')\n"
                                }
                },
                {
                                "id": "agent_16",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 16')\n"
                                }
                },
                {
                                "id": "agent_17",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 17')\n"
                                }
                },
                {
                                "id": "agent_18",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 18')\n"
                                }
                },
                {
                                "id": "agent_19",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 19')\n"
                                }
                },
                {
                                "id": "agent_20",
                                "proposal": {
                                                "path": "app.py",
                                                "affected_symbols": [
                                                                "app.py::FUNC:start"
                                                ],
                                                "content": "def start():\n    print('hotspot agent 20')\n"
                                }
                }
],
            "golden_assertions": {},
            "gate_config": DEFAULT_GATE_CONFIG
        },
        "base": {
            "app.py": "def start():\n    pass\n"
        }
    },

    "class2_obvious_reject": {
        "oracle": {
            "scenario": "class2_obvious_reject",
            "expected_outcome": "REJECTED",
            "expected_reason": "synthesis_failure",
            "agents": [
                {
                    "id": "agent_1",
                    "proposal": {
                        "path": "app.py",
                        "affected_symbols": ["app.py::FUNC:start"],
                        "content": "def start():\n    print('agent 1')\n"
                    }
                },
                {
                    "id": "agent_2",
                    "proposal": {
                        "path": "app.py",
                        "affected_symbols": ["app.py::FUNC:start"],
                        "content": "def start():\n    print('agent 2')\n"
                    }
                }
            ],
            "golden_assertions": {
                "app.py": {
                    "must_contain": [],
                    "must_not_contain": ["agent 1", "agent 2"]
                }
            },
            "gate_config": DEFAULT_GATE_CONFIG
        },
        "base": {"app.py": "def start():\n    pass\n"}
    },
    "class3": {
        "oracle": {
            "scenario": "class3",
            "expected_outcome": "PROMOTED",
            "expected_reason": "sequential_degradation_success",
            "agents": [
                {
                    "id": "agent_1",
                    "proposal": {
                        "path": "lib.py",
                        "affected_symbols": ["lib.py::FUNC:add"],
                        "content": "def add(a, b, c):\n    return a + b + c\n"
                    }
                },
                {
                    "id": "agent_2",
                    "proposal": {
                        "path": "app.py",
                        "affected_symbols": ["app.py::FUNC:main"],
                        "content": "from lib import add\ndef main():\n    add(1, 2)\n    print('Agent 2 added this!')\n\nif __name__ == '__main__':\n    main()\n"
                    }
                }
            ],
            "golden_assertions": {
                "app.py": {
                    "must_contain": ["Agent 2 added this!"],
                    "must_not_contain": []
                },
                "lib.py": {
                    "must_contain": ["def add(a, b):"],
                    "must_not_contain": ["def add(a, b, c):"]
                }
            },
            "gate_config": DEFAULT_GATE_CONFIG
        },
        "base": {
            "lib.py": "def add(a, b):\n    return a + b\n",
            "app.py": "from lib import add\ndef main():\n    add(1, 2)\n\nif __name__ == '__main__':\n    main()\n"
        }
    },
    "oracle_false_green": {
        "oracle": {
            "scenario": "oracle_false_green",
            "expected_outcome": "PROMOTED",
            "expected_reason": "synthesis_success",
            "agents": [
                {
                    "id": "agent_1",
                    "proposal": {
                        "path": "config.py",
                        "affected_symbols": ["config.py::CLASS:Config"],
                        "content": "class Config:\n    host = 'localhost'\n    port = 80\n"
                    }
                }
            ],
            "golden_assertions": {
                "config.py": {
                    "must_contain": [],
                    "must_not_contain": ["port = 80"]
                }
            },
            "gate_config": DEFAULT_GATE_CONFIG
        },
        "base": {
            "config.py": "class Config:\n    host = 'localhost'\n",
            "app.py": "import config\nprint('app running successfully')\n"
        }
    },
    "manifest_failure": {
        "oracle": {
            "scenario": "manifest_failure",
            "expected_outcome": "QUARANTINED",
            "expected_reason": "manifest_error",
            "agents": [
                {
                    "id": "agent_1",
                    "proposal": {
                        "path": "package.json",
                        "affected_symbols": ["package.json"],
                        "content": "{\n  \"name\": \"test\",\n  \"version\": \"1.0.0\",\n  \"dependencies\": {\n    \"express\": \"^4.17.1\"\n  }\n}\n"
                    }
                }
            ],
            "golden_assertions": {
                "package.json": {
                    "must_contain": [],
                    "must_not_contain": ["express"]
                }
            },
            "gate_config": {
                "gates": [
                    {
                        "name": "manifest_check",
                        "command": "FAIL_CLOSED: malformed manifest",
                        "timeout_seconds": 10
                    }
                ]
            }
        },
        "base": {
            "package.json": "{\n  \"name\": \"test\"\n}\n"
        }
    },
    "worker_failure": {
        "oracle": {
            "scenario": "worker_failure",
            "expected_outcome": "QUARANTINED",
            "expected_reason": "worker_crash",
            "agents": [
                {
                    "id": "agent_1",
                    "proposal": {
                        "path": "test.py",
                        "affected_symbols": ["test.py"],
                        "content": "print('hello')\n"
                    }
                }
            ],
            "golden_assertions": {
                "test.py": {
                    "must_contain": [],
                    "must_not_contain": ["hello"]
                }
            },
            "gate_config": {
                "gates": [
                    {
                        "name": "crashing_gate",
                        "command": "python3 -c 'import sys; sys.exit(255)'",
                        "timeout_seconds": 10
                    }
                ]
            }
        },
        "base": {
            "test.py": "pass\n"
        }
    }
}

for name, data in fixtures.items():
    fixture_path = os.path.join(fixtures_dir, name)
    os.makedirs(os.path.join(fixture_path, "base"), exist_ok=True)
    os.makedirs(os.path.join(fixture_path, "expected"), exist_ok=True)
    
    with open(os.path.join(fixture_path, "oracle.json"), "w") as f:
        json.dump(data["oracle"], f, indent=2)
        
    for filename, content in data["base"].items():
        with open(os.path.join(fixture_path, "base", filename), "w") as f:
            f.write(content)

print("Generated missing fixtures")
