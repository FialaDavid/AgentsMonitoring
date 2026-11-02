"""Optional Agent2Telegram integration, keyed by config path rather than bot secrets."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

from . import config, detect


def _path(value):
    return str(Path(value).expanduser().absolute())


# Bridge kinds: Agent2Telegram (`a2t`, the default) and Agent2TelegramTopic (`a2tt`).
MODULES = {'a2t':'agent2telegram', 'a2tt':'agent2telegramtopic'}
CONFIG_ENV = {'a2t':'AGENT2TELEGRAM_CONFIG', 'a2tt':'AGENT2TELEGRAMTOPIC_CONFIG'}


def kind_of(spec):
    return spec.get('kind') or 'a2t'


def entry(config_path, python, pythonpath='', state='', kind='a2t'):
    if kind not in MODULES:
        raise ValueError('Bridge kind must be a2t or a2tt.')
    if kind == 'a2tt' and state:
        raise ValueError('A bridge state directory applies to a2t only.')
    path = _path(config_path)
    if not os.path.isabs(python):
        raise ValueError('Bridge interpreter must be an absolute path.')
    if any('\n' in s or '\0' in s for s in (path, python, pythonpath, state)):
        raise ValueError('Bridge paths must not contain control characters.')
    tag = hashlib.sha256(path.encode()).hexdigest()[:12]
    module = MODULES[kind]
    # An a2tt instance lives in <instance>/config.json, so the instance directory names the bot.
    name = 'Telegram Bridge: '+((Path(path).parent.name+' (topics)') if kind == 'a2tt'
                                else (_session_label(path) or Path(path).stem))
    argv = [python, '-m', module, 'run', '--config', path]
    env = [CONFIG_ENV[kind]+'='+path]
    if pythonpath:
        env.append('PYTHONPATH='+pythonpath)
    if state:
        env.append('AGENT2TELEGRAM_STATE='+_path(state))
    log = str(config.state_dir()/('bridge-'+tag+'.log'))
    pattern = r'(^|[ /])'+module+' run --config '+re.escape(path)+r'($|[[:space:]])'
    item = {'name':name, 'pattern':pattern, 'bridge_config':path, 'enabled':True,
            'restart':'nohup '+shlex.join(['env']+env+argv)+' >> '+shlex.quote(log)+' 2>&1 &'}
    return {**item, 'kind':kind} if kind != 'a2t' else item


CARD = 'Telegram Bridge'
LEGACY_NAME = re.compile(r'^Telegram Bridge: .+ \[[0-9a-f]{12}\]$')


def _session_label(path):
    """The bridged tmux session name (a non-secret field) for a readable title."""
    try:
        value = json.loads(Path(path).read_text('utf-8')).get('tmux_session')
    except (OSError, ValueError, AttributeError):
        return ''
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:60]


def process_env(pid):
    """Read only three restart fields; never log or retain the environment."""
    keys = ('PYTHONPATH','AGENT2TELEGRAM_CONFIG','AGENT2TELEGRAM_STATE','AGENT2TELEGRAMTOPIC_CONFIG')
    try:
        raw = Path(f'/proc/{pid}/environ').read_bytes()
        return {key.decode():value.decode('utf-8') for chunk in raw.split(b'\0')
                for key, sep, value in [chunk.partition(b'=')] if sep and key.decode('utf-8','ignore') in keys}
    except (OSError, UnicodeError):
        pass
    if sys.platform == 'darwin':
        r = detect._run(['ps','eww','-p',str(pid),'-o','command='])
        if r and r.returncode == 0:
            return {key:match.group(1) for key in keys if (match := re.search(
                r'(?:^| )'+key+r'=(.*?)(?= [A-Za-z_][A-Za-z0-9_]*=|$)',r.stdout.rstrip()))}
    return None


def process_info(pid, command):
    """Capture only restart-relevant nonsecret env keys, never serialize all environ."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    by_module = {module:kind for kind, module in MODULES.items()}
    i = next((i for i,w in enumerate(words) if w in by_module), -1)
    if i < 0:
        i = next((i for i,w in enumerate(words) if Path(w).name in by_module), -1)
    if i < 0 or words[i+1:i+2] != ['run']:
        return None
    kind = by_module[Path(words[i]).name]
    # Do not mistake a shell containing a quoted command for the actual bridge.
    if Path(words[0]).name in ('sh','bash','zsh','fish'):
        return None
    env = process_env(pid)
    tail = words[i+2:]
    explicit = '--config' in tail or any(w.startswith('--config=') for w in tail)
    if env is None and not explicit:
        return None  # Unknown env-only config cannot safely be treated as default.
    env = env or {}
    # Only a2t has a default config location; an a2tt bridge needs an explicit or env config.
    cfg = env.get(CONFIG_ENV[kind]) or (str(Path.home()/'.config/agent2telegram/config.json')
                                         if kind == 'a2t' else None)
    if '--config' in tail:
        index = tail.index('--config')
        if index+1 >= len(tail):
            return None
        cfg = tail[index+1]
    else:
        cfg = next((w.split('=',1)[1] for w in tail if w.startswith('--config=')), cfg)
    if not cfg:
        return None
    python = words[0] if Path(words[0]).name.startswith('python') else None
    if python and not os.path.isabs(python):
        from .wizard import find_cli
        python = find_cli(python)
    return {'kind':kind, 'config':_path(cfg), 'python':python, 'pythonpath':env.get('PYTHONPATH',''),
            'state':env.get('AGENT2TELEGRAM_STATE','')}


def processes():
    procs, _ = detect._proc_table()
    own = detect._run(['ps','-U',str(os.getuid()),'-o','pid='])
    allowed = {int(x) for x in own.stdout.split() if x.isdigit()} if own and own.returncode == 0 else set()
    return [(pid, info) for pid, cmd in procs.items() if pid in allowed
            if (info := process_info(pid, cmd))]


def running_pids(spec):
    return [pid for pid, info in processes()
            if info['config'] == spec['bridge_config'] and info.get('kind', 'a2t') == kind_of(spec)]


def candidates():
    found = {}
    for _, info in processes():
        if info['python']:
            item = entry(info['config'], info['python'], info['pythonpath'],
                         info['state'] if info['kind'] == 'a2t' else '', info['kind'])
            found[item['bridge_config']] = item
    return list(found.values())


def register(cfg, item):
    """Idempotent shared implementation for interactive add and the public CLI."""
    entries = cfg.setdefault('daemons', [])
    existing = next((d for d in entries if d.get('bridge_config') == item['bridge_config']), None)
    if item['name'] in {d.get('name') for d in entries if d is not existing}:
        # Two bridges for the same session/file name: keep them apart with the path hash.
        item = {**item, 'name':item['name']+' ['+hashlib.sha256(item['bridge_config'].encode()).hexdigest()[:12]+']'}
    if existing:
        # Preserve explicit stopped state and custom names; only automatic titles are renamed.
        keep = item['name'] if LEGACY_NAME.match(existing.get('name', '')) else existing['name']
        item = {**item, 'enabled':existing.get('enabled', True), 'name':keep}
        existing.update(item)
    else:
        entries.append(item)
    cfg['daemons'] = [d for d in entries if not (d.get('name') == 'Telegram Bridge'
                     and d.get('pattern') == 'agent2telegram run')]
    # One card for all bridges. It is up only when every enabled bridge runs, so a healthy bot
    # never hides a dead one; the detail names the missing bridge. Per-bridge cards and the
    # legacy shared card are folded into it.
    services = [s for s in cfg.get('services', [])
                if not s.get('bridge_config') and s.get('name') != 'Telegram Bridge Status']
    if not any(s.get('kind') == 'bridges' for s in services):
        services.append({'name':CARD, 'kind':'bridges'})
    cfg['services'] = services


def status_file(spec):
    """Where an a2tt bridge writes its status: ``status_file`` in the spec, else
    ``<instance>/state/<Bot>.status.json`` next to ``<instance>/config.json``."""
    if spec.get('status_file'):
        return Path(spec['status_file']).expanduser()
    instance = Path(spec['bridge_config']).parent
    return instance/'state'/(instance.name+'.status.json')


def read_status(spec):
    """The parsed status file, or None when it is missing or unreadable (never fatal)."""
    try:
        data = json.loads(status_file(spec).read_text('utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stale_seconds(data, now):
    """Seconds since the last heartbeat when it is older than the contract allows
    (3 heartbeats + 30 s), else None. A file without a usable timestamp says nothing."""
    updated, beat = data.get('updated_at'), data.get('heartbeat_seconds')
    if type(updated) is not int:
        return None
    if type(beat) not in (int, float) or beat <= 0:
        beat = 15
    age = now - updated
    return int(age) if age > 3*beat+30 else None


def state(spec, now=None):
    """('running' | 'down' | 'stale', note) for one configured bridge. Stale means the process
    is there but an a2tt bridge stopped refreshing its status file (hung)."""
    if not running_pids(spec):
        return 'down', ''
    if kind_of(spec) == 'a2tt':
        data = read_status(spec)
        late = stale_seconds(data, time.time() if now is None else now) if data else None
        if late is not None:
            return 'stale', f'no heartbeat for {late} s'
    return 'running', ''


def states(cfg):
    """[(spec, state, note)] for every configured, enabled bridge. A kind that is not
    configured does not appear, so it can never count as an outage."""
    return [(d, *state(d)) for d in cfg.get('daemons', []) if d.get('bridge_config') and d.get('enabled', True)]


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _model_label(value):
    if not isinstance(value, str) or not value:
        return '—'
    match = re.fullmatch(r'claude-([a-z]+)-(\d+)-(\d+)(?:-.*)?', value)
    return match.group(1)[0].upper()+match.group(2)+'.'+match.group(3) if match else value


def _age(stamp, now):
    return max(0, int(now-stamp)) if type(stamp) in (int, float) else None


def topics(spec, now=None):
    """Read-only view of the topics (lanes) an a2tt bridge reports, or None without a status file.
    Only display fields are kept; chat ids, paths and session names are dropped."""
    data = read_status(spec)
    if data is None or not isinstance(data.get('lanes'), list):
        return None
    now = time.time() if now is None else now
    items = []
    for lane in data['lanes'][:100]:
        if not isinstance(lane, dict):
            continue
        used, window = _count(lane.get('ctx_tokens')), _count(lane.get('ctx_window'))
        title = lane.get('title')
        model = lane.get('model')
        model = model[:40] if isinstance(model, str) else None
        items.append({'thread':lane.get('thread'), '_tmux_session':lane.get('tmux_session'),
                      'title':' '.join(title.split())[:80] if isinstance(title, str) else None,
                      'agent':str(lane.get('agent') or '')[:30],
                      'status':str(lane.get('status') or '')[:20],
                      'alive':lane.get('alive') is True, 'busy':lane.get('busy') is True,
                      'last_activity_age':_age(lane.get('last_activity'), now),
                      'last_compact_age':_age(lane.get('last_compact'), now),
                      'ctx_tokens':used, 'ctx_window':window,
                      'model':model, 'model_label':_model_label(model),
                      'ctx_percent':round(100*used/window, 1) if used is not None and window else None})
    return {'items':items, 'stale':stale_seconds(data, now) is not None,
            'tmux_tmpdir':data.get('tmux_tmpdir')}


def master_specs(cfg):
    """Enabled a2tt bridges that name the agent whose row should carry their topics."""
    return [d for d in cfg.get('daemons', []) if d.get('bridge_config') and d.get('enabled', True)
            and kind_of(d) == 'a2tt' and d.get('master')]


def health(cfg):
    """(up, detail) for the shared card; None when no bridge is configured."""
    found = states(cfg)
    if not found:
        return None
    good = [d['name'] for d, st, _ in found if st == 'running']
    detail = f"{len(good)}/{len(found)} bridges running"
    for label in ('down', 'stale'):
        names = [d['name'] for d, st, _ in found if st == label]
        if names:
            detail += f'; {label}: '+', '.join(names)
    known = {a.get('name') for a in cfg.get('agents', [])}
    for d in master_specs(cfg):
        if d['master'] not in known:
            detail += f"; master {d['master']} not found"
    return len(good) == len(found), detail


def rows(cfg):
    """One row per configured bridge for the shared card, in the module-row format."""
    return [{'name':d['name'], 'up':st == 'running', 'latency':None,
             'detail':(st+(' – '+note if note else '')), 'url':''} for d, st, note in states(cfg)]


def add_noninteractive(args):
    try:
        item = entry(args.bridge_config, args.bridge_python, args.bridge_pythonpath or '', args.bridge_state or '',
                     getattr(args, 'bridge_kind', None) or 'a2t')
        cfg = config.load()
        register(cfg, item)
        config.save(cfg)
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)}))
        return 1
    print(json.dumps({'ok':True,'name':item['name']}))
    return 0
