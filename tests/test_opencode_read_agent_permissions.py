"""Match installed OpenCode legacy normalization and ordered permission checks.

These are controlled configuration regressions, not client execution. OpenCode
1.17.20 converts legacy agent.tools to a permission object before Object.assign
with explicit permissions; overriding a key retains its insertion position.
"""
from __future__ import annotations

import fnmatch
import json
import os

import pytest

from test_live_agent_read_qualification import helper

pytestmark = pytest.mark.scope_policy


def normalized_agent_rules(agent):
    # Installed dH AgentConfig transform followed by Permission.fromConfig.
    permissions = {}
    for tool, allowed in agent.get('tools', {}).items():
        name = 'edit' if tool in {'write', 'edit', 'patch'} else tool
        permissions[name] = 'allow' if allowed else 'deny'
    permissions.update(agent['permission'])
    rules = []
    for tool, action in permissions.items():
        if isinstance(action, str):
            rules.append((tool, '*', action))
        else:
            rules.extend((tool, pattern, decision) for pattern, decision in action.items())
    return rules


def tool_is_visible(tool, rules):
    # Installed Permission.disabled uses the last rule for the permission
    # name; only an unconditional deny removes a tool from the model payload.
    matches = [rule for rule in rules if fnmatch.fnmatchcase(tool, rule[0])]
    return not matches or matches[-1][1:] != ('*', 'deny')


def action_for(tool, path, rules):
    matches = [rule for rule in rules if fnmatch.fnmatchcase(tool, rule[0])
               and fnmatch.fnmatchcase(path, rule[1])]
    return matches[-1][2] if matches else 'ask'


def configured_agent(tmp_path):
    fixture = tmp_path / 'qualification.txt'
    helper.client_command('opencode', 'http://127.0.0.1:19999', 'explicit-model',
                          'dummy-local-token', tmp_path, fixture)
    config = json.loads((tmp_path / 'opencode.json').read_text())
    return fixture, config, config['agent']['qualification_reader']


def test_exact_read_permission_keeps_tool_visible_after_client_normalization(tmp_path):
    fixture, config, agent = configured_agent(tmp_path)
    assert 'tools' not in agent
    assert agent['mode'] == 'primary'
    assert list(agent['permission']) == ['*', 'read']
    assert config['permission'] == agent['permission']
    rules = normalized_agent_rules(agent)
    assert tool_is_visible('read', rules)
    assert action_for('read', str(fixture), rules) == 'allow'
    assert action_for('read', os.path.relpath(fixture, '/'), rules) == 'allow'
    assert action_for('read', fixture.name, rules) == 'deny'
    assert action_for('read', str(tmp_path / 'other.txt'), rules) == 'deny'
    assert action_for('read', str(tmp_path.parent / 'qualification.txt'), rules) == 'deny'
    for tool in ('bash', 'edit', 'write', 'apply_patch', 'glob', 'grep', 'task',
                 'webfetch', 'websearch', 'lsp', 'skill', 'question', 'todowrite'):
        assert not tool_is_visible(tool, rules)
        assert action_for(tool, '*', rules) == 'deny'


def test_legacy_read_toggle_appends_wildcard_deny_and_reproduces_hidden_tool(tmp_path):
    fixture, _, agent = configured_agent(tmp_path)
    legacy_agent = dict(agent, tools={'read': True, 'bash': False, 'write': False})
    rules = normalized_agent_rules(legacy_agent)
    assert rules[-1] == ('*', '*', 'deny')
    assert not tool_is_visible('read', rules)
    assert action_for('read', str(fixture), rules) == 'deny'


def test_read_tool_global_non_git_worktree_pattern_allows_only_the_selected_fixture(tmp_path):
    fixture, config, agent = configured_agent(tmp_path)
    command, _ = helper.client_command('opencode', 'http://127.0.0.1:19999', 'explicit-model',
                                       'dummy-local-token', tmp_path, fixture)
    assert command[command.index('--dir') + 1] == str(tmp_path)
    assert list(agent['permission']['read']) == ['*', str(fixture), os.path.relpath(fixture, '/')]
    assert config['permission'] == agent['permission']
    rules = normalized_agent_rules(agent)

    # Installed Project.fromDirectory returns worktree '/' for global non-Git
    # projects. --dir sets instance.directory, not this instance.worktree.
    # ReadTool then asks with path.relative(worktree, resolvedFile).
    assert not (tmp_path / '.git').exists()
    worktree = '/'
    pattern = os.path.relpath(fixture.resolve(), worktree)
    assert pattern == str(fixture).lstrip('/')
    assert pattern != fixture.name
    assert action_for('read', pattern, rules) == 'allow'
    absolute_only = dict(agent, permission={'*': 'deny', 'read': {'*': 'deny', str(fixture): 'allow'}})
    assert action_for('read', pattern, normalized_agent_rules(absolute_only)) == 'deny'

    for other_file in (tmp_path / 'other.txt', tmp_path / 'opencode.json',
                       tmp_path / 'subdirectory' / fixture.name,
                       tmp_path.parent / fixture.name,
                       tmp_path.parent / 'other-directory' / fixture.name):
        denied_pattern = os.path.relpath(other_file.resolve(), worktree)
        assert action_for('read', denied_pattern, rules) == 'deny'
    assert action_for('read', fixture.name, rules) == 'deny'
    assert action_for('read', os.path.relpath(fixture.resolve(), tmp_path.resolve()), rules) == 'deny'
    assert action_for('read', os.path.relpath(fixture.resolve(),
                      (tmp_path.parent / 'different-worktree').resolve()), rules) == 'deny'
    for other_tool in ('bash', 'write', 'edit', 'glob', 'grep'):
        assert action_for(other_tool, pattern, rules) == 'deny'
