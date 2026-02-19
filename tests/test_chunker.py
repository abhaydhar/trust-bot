"""Tests for the code chunker."""

from pathlib import Path

from trustbot.indexing.chunker import chunk_file


SAMPLE_PYTHON = '''\
"""Module docstring."""

import os


class MyService:
    def __init__(self, config):
        self.config = config

    def process(self, data):
        result = self.validate(data)
        return self.transform(result)

    def validate(self, data):
        if not data:
            raise ValueError("No data")
        return data

    def transform(self, data):
        return {"processed": data}


def helper_function(x):
    return x * 2
'''


def test_chunk_python_file(tmp_path: Path):
    file = tmp_path / "service.py"
    file.write_text(SAMPLE_PYTHON)

    chunks = chunk_file(file, tmp_path)

    assert len(chunks) > 0

    # Should find the class and its methods
    names = {c.function_name for c in chunks}
    assert "MyService" in names or "process" in names
    assert "helper_function" in names

    # All chunks should have the correct file path
    for chunk in chunks:
        assert chunk.file_path == "service.py"
        assert chunk.language == "python"


SAMPLE_JAVA = '''\
package com.example;

public class UserService {
    private final UserRepository repo;

    public UserService(UserRepository repo) {
        this.repo = repo;
    }

    public User findUser(String id) {
        return repo.findById(id);
    }

    public void deleteUser(String id) {
        repo.delete(id);
    }
}
'''


def test_chunk_java_file(tmp_path: Path):
    file = tmp_path / "UserService.java"
    file.write_text(SAMPLE_JAVA)

    chunks = chunk_file(file, tmp_path)

    assert len(chunks) > 0
    names = {c.function_name for c in chunks}
    # Should find at least some of the methods
    assert len(names) >= 1
    for chunk in chunks:
        assert chunk.language == "java"


SAMPLE_JS = '''\
const express = require("express");

function handleRequest(req, res) {
    const data = parseBody(req);
    res.json(data);
}

const parseBody = (req) => {
    return JSON.parse(req.body);
};

class Router {
    constructor() {
        this.routes = [];
    }

    addRoute(path, handler) {
        this.routes.push({ path, handler });
    }
}
'''


def test_chunk_js_file(tmp_path: Path):
    file = tmp_path / "server.js"
    file.write_text(SAMPLE_JS)

    chunks = chunk_file(file, tmp_path)

    assert len(chunks) > 0
    names = {c.function_name for c in chunks}
    assert "handleRequest" in names
    for chunk in chunks:
        assert chunk.language == "javascript"
