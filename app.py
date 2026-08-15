import eventlet
eventlet.monkey_patch()

import os
import re
import random

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-tajne-palermo-heslo')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

NAME_PATTERN = re.compile(r'^[^\s<>"\'&/\\]+(?: [^\s<>"\'&/\\]+)*$', re.UNICODE)
MAX_NAME_LENGTH = 15

game_state = {
    "players": {},
    "host_sid": None,
    "phase": "Lobby",
    "votes": {},
    "night_actions": {},
    "settings": {}
}

ALL_ROLES = ["Měšťan", "Mafián", "Policista", "Stopař", "Pastičkář", "Doktor", "Detektiv", "Šašek", "Blázen"]

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    emit('update_players', get_player_info())

@socketio.on('reconnect_player')
def handle_reconnect(data):
    name = data.get('name')
    new_sid = request.sid

    for old_sid, p in list(game_state["players"].items()):
        if p["name"] == name:
            # Převedeme všechna stará data pod nové spojení
            if old_sid != new_sid:
                p["connected"] = True
                game_state["players"][new_sid] = p
                del game_state["players"][old_sid]

                if game_state["host_sid"] == old_sid:
                    game_state["host_sid"] = new_sid

                if old_sid in game_state["votes"]:
                    game_state["votes"][new_sid] = game_state["votes"].pop(old_sid)
                if old_sid in game_state["night_actions"]:
                    game_state["night_actions"][new_sid] = game_state["night_actions"].pop(old_sid)

            # Pošleme klientovi aktuální stav, aby mu nezamrzla obrazovka
            resend_state_to_player(new_sid)
            emit('update_players', get_player_info(), broadcast=True)
            
            # Zkontrolujeme, jestli jeho návrat něco neovlivnil
            resolve_round_after_removal()
            return

def resend_state_to_player(sid):
    p = game_state["players"].get(sid)
    if not p: return
    phase = game_state["phase"]

    emit('join_success', {'name': p["name"]}, to=sid)
    emit('host_status', {'is_host': (game_state["host_sid"] == sid)}, to=sid)

    if phase == "Noc":
        alive_players = [p2["name"] for p2 in game_state["players"].values() if p2["alive"]]
        mafia_names = [p2["name"] for p2 in game_state["players"].values() if p2["actual_role"] == "Mafián" and p2["alive"]]
        mates = [m for m in mafia_names if m != p["name"]] if p["actual_role"] == "Mafián" else []
        all_roles = [{"name": p2["name"], "role": p2["actual_role"], "alive": p2["alive"]} for p2 in game_state["players"].values()]

        payload = {
            "role": p["perceived_role"],
            "phase": phase,
            "alive": p["alive"],
            "alive_players": alive_players,
            "mafia_mates": mates
        }
        if not p["alive"]: payload["all_roles"] = all_roles
        emit('game_started', payload, to=sid)
        
    elif phase == "Den":
        all_roles = [{"name": p2["name"], "role": p2["actual_role"], "alive": p2["alive"]} for p2 in game_state["players"].values()]
        payload = {'msg': "Znovu ses připojil! Obrazovka je synchronizována.", 'dead': False, 'personal_msgs': [], 'is_alive': p["alive"]}
        if not p["alive"]: payload["all_roles"] = all_roles
        emit('day_phase', payload, to=sid)
        
    elif phase == "Hlasování":
        alive_players = [p2["name"] for p2 in game_state["players"].values() if p2["alive"]]
        all_roles = [{"name": p2["name"], "role": p2["actual_role"], "alive": p2["alive"]} for p2 in game_state["players"].values()]
        payload = {'candidates': alive_players}
        if not p["alive"]: payload["all_roles"] = all_roles
        emit('voting_started', payload, to=sid)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in game_state["players"]:
        game_state["players"][sid]["connected"] = False
        
        # Pokud se odpojil Host, předáme korunu prvnímu živému spojení
        if game_state["host_sid"] == sid:
            connected_sids = [s for s, p in game_state["players"].items() if p.get("connected", True) and s != sid]
            if connected_sids:
                game_state["host_sid"] = connected_sids[0]
                emit('host_status', {'is_host': True}, to=game_state["host_sid"])
        
        emit('update_players', get_player_info(), broadcast=True)
        # Hned zjistíme, jestli hra nečekala už jen na něj
        resolve_round_after_removal()

def remove_player(sid):
    if sid not in game_state["players"]:
        return False

    del game_state["players"][sid]
    game_state["night_actions"].pop(sid, None)
    game_state["votes"].pop(sid, None)

    if len(game_state["players"]) == 0:
        game_state["phase"] = "Lobby"
        game_state["host_sid"] = None
        game_state["votes"] = {}
        game_state["night_actions"] = {}
    elif game_state["host_sid"] == sid:
        connected_sids = [s for s, p in game_state["players"].items() if p.get("connected", True)]
        game_state["host_sid"] = connected_sids[0] if connected_sids else None
        if game_state["host_sid"]:
            emit('host_status', {'is_host': True}, to=game_state["host_sid"])

    return True

def resolve_round_after_removal():
    if len(game_state["players"]) == 0 or game_state["phase"] == "Lobby":
        return

    winner, default_msg = get_winner()
    if winner:
        emit('game_over', {'winner': winner, 'msg': default_msg}, broadcast=True)
        game_state["phase"] = "Lobby"
    elif game_state["phase"] == "Noc":
        check_night_end()
    elif game_state["phase"] == "Hlasování":
        evaluate_votes_if_ready()

@socketio.on('join_game')
def handle_join(data):
    name = data.get('name', '').strip()
    if not name:
        emit('error_msg', 'Jméno nesmí být prázdné!', to=request.sid)
        return

    if len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.match(name):
        emit('error_msg', f'Jméno smí mít max. {MAX_NAME_LENGTH} znaků a nesmí obsahovat speciální znaky.', to=request.sid)
        return

    if game_state["phase"] != "Lobby":
        emit('error_msg', 'Hra už probíhá! Musíš počkat, až skončí.', to=request.sid)
        return

    existing_names = [p["name"].lower() for p in game_state["players"].values()]
    if name.lower() in existing_names:
        emit('error_msg', f'Jméno "{name}" už v lobby je. Zvol si jiné!', to=request.sid)
        return

    sid = request.sid
    game_state["players"][sid] = {
        "name": name,
        "actual_role": "Měšťan",
        "perceived_role": "Měšťan",
        "alive": True,
        "connected": True
    }
    
    if not game_state["host_sid"]:
        game_state["host_sid"] = sid
    
    emit('join_success', {'name': name}, to=sid)
    emit('host_status', {'is_host': (game_state["host_sid"] == sid)}, to=sid)
    emit('update_players', get_player_info(), broadcast=True)

@socketio.on('kick_player')
def handle_kick(data):
    if request.sid != game_state["host_sid"]: return 
    
    target_name = data.get('name')
    target_sid = None
    
    for sid, p in game_state["players"].items():
        if p["name"] == target_name:
            target_sid = sid
            break
            
    if target_sid:
        emit('kicked', {'msg': 'Byl jsi vyhozen z Lobby administrátorem.'}, to=target_sid)
        remove_player(target_sid)
        emit('update_players', get_player_info(), broadcast=True)
        resolve_round_after_removal()

@socketio.on('start_game')
def handle_start(data):
    if request.sid != game_state["host_sid"]: return
    
    game_state["phase"] = "Lobby"
    game_state["votes"] = {}
    game_state["night_actions"] = {}
    
    total_players = len(game_state["players"])

    if total_players < 3:
        emit('error_msg', 'Pro odstartování hry jsou potřeba aspoň 3 hráči!', to=request.sid)
        return

    settings = {
        'mafia': data.get('mafia', '1'),
        'pol': data.get('pol', '0'),
        'track': data.get('track', '0'),
        'trap': data.get('trap', '0'),
        'doc': data.get('doc', '0'),
        'det': data.get('det', '0'),
        'jester': data.get('jester', '0'),
        'insane': data.get('insane', '0')
    }

    max_limits = {
        'mafia': max(1, total_players // 3),
        'pol': 2,
        'track': 2,
        'trap': 2,
        'doc': 2,
        'det': max(1, total_players // 4),
        'jester': 1,
        'insane': 2
    }

    assigned_counts = {}
    random_roles = []
    fixed_sum = 0

    for role_key, val in settings.items():
        if val == 'random':
            assigned_counts[role_key] = 1
            random_roles.append(role_key)
            fixed_sum += 1
        else:
            try:
                c = max(0, int(val))
            except (TypeError, ValueError):
                c = 0
            assigned_counts[role_key] = c
            fixed_sum += c

    if fixed_sum > total_players:
        emit('error_msg', f'Pro toto nastavení potřebujete aspoň {fixed_sum} hráčů! Máte {total_players}.', to=request.sid)
        return

    available_for_random = total_players - fixed_sum
    if available_for_random > 0 and random_roles:
        max_possible_additions = sum(max_limits[r] - 1 for r in random_roles if max_limits[r] > 1)
        if max_possible_additions > 0:
            extra_to_add = random.randint(0, min(available_for_random, max_possible_additions))
            for _ in range(extra_to_add):
                valid_candidates = [r for r in random_roles if assigned_counts[r] < max_limits[r]]
                if not valid_candidates: break
                assigned_counts[random.choice(valid_candidates)] += 1

    game_state["settings"]["mafia_consensus"] = data.get('mafia_consensus', False)
    game_state["settings"]["public_voting"] = data.get('public_voting', False)
    game_state["settings"]["reveal_roles"] = data.get('reveal_roles', True)

    roles_to_assign = (
        ["Mafián"] * assigned_counts['mafia'] + 
        ["Policista"] * assigned_counts['pol'] + 
        ["Stopař"] * assigned_counts['track'] + 
        ["Pastičkář"] * assigned_counts['trap'] +
        ["Doktor"] * assigned_counts['doc'] + 
        ["Detektiv"] * assigned_counts['det'] + 
        ["Šašek"] * assigned_counts['jester'] + 
        ["Blázen"] * assigned_counts['insane']
    )
    
    while len(roles_to_assign) < total_players:
        roles_to_assign.append("Měšťan")
        
    random.shuffle(roles_to_assign)
    p_ids = list(game_state["players"].keys())
    
    for idx, sid in enumerate(p_ids):
        r = roles_to_assign[idx]
        p = game_state["players"][sid]
        p["actual_role"] = r
        p["alive"] = True
        p["perceived_role"] = random.choice(["Policista", "Stopař", "Pastičkář", "Doktor", "Detektiv"]) if r == "Blázen" else r

    start_night()

def start_night():
    game_state["phase"] = "Noc"
    game_state["night_actions"] = {}
    
    alive_players = [p["name"] for p in game_state["players"].values() if p["alive"]]
    mafia_names = [p["name"] for p in game_state["players"].values() if p["actual_role"] == "Mafián" and p["alive"]]
    all_roles_payload = [{"name": p["name"], "role": p["actual_role"], "alive": p["alive"]} for p in game_state["players"].values()]
    
    for sid, player in game_state["players"].items():
        mates = [m for m in mafia_names if m != player["name"]] if player["actual_role"] == "Mafián" else []
        
        payload = {
            "role": player["perceived_role"],
            "phase": game_state["phase"],
            "alive": player["alive"],
            "alive_players": alive_players,
            "mafia_mates": mates
        }
        if not player["alive"]: payload["all_roles"] = all_roles_payload
        if player.get("connected", True):
            emit('game_started', payload, to=sid)

@socketio.on('proceed_to_night')
def handle_proceed_to_night():
    if game_state["phase"] != "Lobby" and request.sid == game_state["host_sid"]:
        start_night()

@socketio.on('night_action')
def handle_night_action(data):
    if game_state["phase"] != "Noc": return
    sid = request.sid
    if sid not in game_state["players"] or not game_state["players"][sid]["alive"]: return
    
    game_state["night_actions"][sid] = data.get('target')
    emit('action_confirmed', {'msg': 'Akce odeslána. Čeká se na ostatní...'}, to=sid)
    check_night_end()

def check_night_end():
    alive_sids = [s for s, p in game_state["players"].items() if p["alive"]]
    connected_alive_sids = [s for s in alive_sids if game_state["players"][s].get("connected", True)]
    
    if not connected_alive_sids: return
    
    actions_submitted = [s for s in game_state["night_actions"] if s in connected_alive_sids]
    if len(actions_submitted) < len(connected_alive_sids): return

    alive_names = [game_state["players"][s]["name"] for s in alive_sids]

    actions = {}
    for sid, target in game_state["night_actions"].items():
        player = game_state["players"].get(sid)
        if not player: continue
        actions[sid] = {
            "name": player["name"], "role": player["actual_role"], "perc_role": player["perceived_role"],
            "target": target, "blocked": False, "trapped": False
        }

    name_to_sid = {p["name"]: s for s, p in game_state["players"].items()}
    personal_msgs = {s: [] for s in alive_sids}
    
    def add_msg(target_sid, msg_type, icon, title, text):
        if target_sid in personal_msgs:
            personal_msgs[target_sid].append({'type': msg_type, 'icon': icon, 'title': title, 'text': text})

    for sid, act in actions.items():
        if act["role"] == "Policista" and act["target"]:
            tgt_sid = name_to_sid.get(act["target"])
            if tgt_sid in actions:
                tgt_act = actions[tgt_sid]
                tried_to_leave = bool(tgt_act["target"]) and tgt_act["role"] not in ["Měšťan", "Šašek"]
                if tried_to_leave:
                    tgt_act["blocked"] = True
                    add_msg(tgt_sid, 'danger', 'fa-handcuffs', 'Zásah policie!', 'Zastavila tě policie! Tvá noční akce byla zrušena a zůstal jsi doma.')
                    add_msg(sid, 'success', 'fa-user-lock', 'Úspěšný zásah!', f'Úspěšně jsi zablokoval hráče <b>{act["target"]}</b>, který se zrovna chystal odejít z domu!')
                else:
                    add_msg(sid, 'info', 'fa-user-shield', 'Klidná hlídka', f'Hlídal jsi hráče <b>{act["target"]}</b>, ale ten celou noc nevyšel z domu.')

    for sid, act in actions.items():
        if act["role"] == "Stopař" and not act["blocked"] and act["target"]:
            tgt_sid = name_to_sid.get(act["target"])
            visited_target = None
            if tgt_sid in actions and not actions[tgt_sid]["blocked"] and actions[tgt_sid]["role"] not in ["Měšťan", "Šašek"]:
                visited_target = actions[tgt_sid]["target"]
            if visited_target: add_msg(sid, 'success', 'fa-shoe-prints', 'Stopy nalezeny!', f'Hráč <b>{act["target"]}</b> navštívil hráče: <b>{visited_target}</b>.')
            else: add_msg(sid, 'info', 'fa-shoe-prints', 'Čistá stopa', f'Hráč <b>{act["target"]}</b> zůstal celou noc doma.')

    traps = {} 
    for sid, act in actions.items():
        if act["role"] == "Pastičkář" and not act["blocked"] and act["target"]:
            traps.setdefault(act["target"], []).append(sid)

    visitors = [(v_sid, v_act) for v_sid, v_act in actions.items() if v_act["role"] in ["Mafián", "Doktor", "Detektiv"] and not v_act["blocked"] and v_act["target"] in traps]
    random.shuffle(visitors)

    traps_triggered = {t: [] for t in traps}
    active_traps = {t: len(traps[t]) for t in traps} 
    
    for v_sid, v_act in visitors:
        tgt = v_act["target"]
        if active_traps[tgt] > 0:
            v_act["trapped"] = True
            active_traps[tgt] -= 1
            traps_triggered[tgt].append(v_act["name"])
            add_msg(v_sid, 'danger', 'fa-spider', 'Past!', 'Šlápl jsi do pasti! Tvá akce byla přerušena.')

    for tgt_house, t_sids in traps.items():
        caught_names = traps_triggered[tgt_house]
        for i, t_sid in enumerate(t_sids):
            if i < len(caught_names): add_msg(t_sid, 'success', 'fa-spider', 'Past sklapla!', f'V pasti u hráče <b>{tgt_house}</b> uvízl: <b>{caught_names[i]}</b>')
            else: add_msg(t_sid, 'info', 'fa-spider', 'Klidná past', f'Do tvé pasti u hráče <b>{tgt_house}</b> nikdo nešlápl.')

    dead_names, healed_names, mafia_votes = set(), set(), []
    for sid, act in actions.items():
        if act["blocked"] or act["trapped"] or not act["target"]: continue
        if act["role"] == "Doktor": healed_names.add(act["target"])
        elif act["role"] == "Detektiv":
            tgt_real = next((p["actual_role"] for p in game_state["players"].values() if p["name"] == act["target"]), None)
            if tgt_real is None: continue
            shown = [tgt_real, random.choice([r for r in ALL_ROLES if r != tgt_real])]
            random.shuffle(shown)
            add_msg(sid, 'success', 'fa-magnifying-glass', 'Stopy', f'<b>{act["target"]}</b> je <b>{shown[0]}</b> NEBO <b>{shown[1]}</b>!')
        elif act["role"] == "Mafián": mafia_votes.append(act["target"])

    if mafia_votes:
        if game_state["settings"]["mafia_consensus"]:
            if len(set(mafia_votes)) == 1: 
                dead_names.add(mafia_votes[0])
                for sid in [s for s, a in actions.items() if a["role"] == "Mafián" and not a["blocked"] and not a["trapped"]]:
                    add_msg(sid, 'info', 'fa-user-secret', 'Útok mafie', f'Úspěšně jste zaútočili na <b>{mafia_votes[0]}</b>.')
            else:
                for sid in [s for s, a in actions.items() if a["role"] == "Mafián"]:
                    add_msg(sid, 'warning', 'fa-triangle-exclamation', 'Neshoda!', 'Neshodli jste se, útok zrušen.')
        else:
            target = max(set(mafia_votes), key=mafia_votes.count)
            dead_names.add(target)
            for sid in [s for s, a in actions.items() if a["role"] == "Mafián" and not a["blocked"] and not a["trapped"]]:
                add_msg(sid, 'info', 'fa-user-secret', 'Útok mafie', f'Zaútočili jste na <b>{target}</b>.')

    actual_deaths = []
    for n in dead_names:
        if n in healed_names:
            for sid, act in actions.items():
                if act["role"] == "Doktor" and act["target"] == n and not act["blocked"] and not act["trapped"]:
                    add_msg(sid, 'success', 'fa-heart-pulse', 'Život zachráněn!', f'Tvůj včasný zásah zachránil <b>{n}</b> před mafií!')
        else: actual_deaths.append(n)

    for sid, act in actions.items():
        if act["role"] == "Doktor" and act["target"] and act["target"] not in dead_names and not act["blocked"] and not act["trapped"]:
            add_msg(sid, 'info', 'fa-syringe', 'Léčení', f'Hlídal jsi hráče <b>{act["target"]}</b>.')

    for sid, act in actions.items():
        if act["role"] == "Blázen" and act["target"] and not act["blocked"]:
            pr = act["perc_role"]
            if pr == "Detektiv":
                shown = random.sample(ALL_ROLES, 2)
                add_msg(sid, 'success', 'fa-magnifying-glass', 'Stopy', f'<b>{act["target"]}</b> je <b>{shown[0]}</b> NEBO <b>{shown[1]}</b>!')
            elif pr == "Stopař":
                if random.choice([True, False]): add_msg(sid, 'success', 'fa-shoe-prints', 'Stopy!', f'<b>{act["target"]}</b> navštívil: <b>{random.choice(alive_names)}</b>.')
                else: add_msg(sid, 'info', 'fa-shoe-prints', 'Čistá stopa', f'<b>{act["target"]}</b> zůstal doma.')
            elif pr == "Pastičkář":
                if random.choice([True, False]): add_msg(sid, 'success', 'fa-spider', 'Past sklapla!', f'Někdo se v noci chytil do tvé pasti! Byl to: <b>{random.choice(alive_names)}</b>')
                else: add_msg(sid, 'info', 'fa-spider', 'Klidná past', f'Do tvé pasti u hráče <b>{act["target"]}</b> nikdo nešlápl.')
            elif pr == "Policista":
                if random.choice([True, False]): add_msg(sid, 'success', 'fa-user-lock', 'Úspěšný zásah!', f'Úspěšně jsi zablokoval hráče <b>{act["target"]}</b>!')
                else: add_msg(sid, 'info', 'fa-user-shield', 'Klidná hlídka', f'Hlídal jsi hráče <b>{act["target"]}</b>, ale nikam nešel.')
            elif pr == "Doktor":
                if random.random() < 0.2: add_msg(sid, 'success', 'fa-heart-pulse', 'Život zachráněn!', f'Tvůj včasný zásah zachránil <b>{act["target"]}</b>!')
                else: add_msg(sid, 'info', 'fa-syringe', 'Léčení', f'Hlídal jsi hráče <b>{act["target"]}</b>.')

    for sid, act in actions.items():
        if not act["target"] and not act["blocked"] and act["role"] in ["Měšťan", "Šašek"]:
            add_msg(sid, 'info', 'fa-bed', 'Poklidný spánek', 'Spal jsi ve své posteli.')

    for p in game_state["players"].values():
        if p["name"] in actual_deaths: p["alive"] = False

    if check_win_condition(is_night=True): return 

    dead_msg_list = []
    for name in actual_deaths:
        p = next((p for p in game_state["players"].values() if p["name"] == name), None)
        if p is None: continue
        r_str = f" <span class='text-slate-400 font-normal italic'>(Byl to: {p['actual_role']})</span>" if game_state["settings"]["reveal_roles"] else ""
        dead_msg_list.append(f"<div class='text-xl font-bold text-white'>{name}{r_str}</div>")

    msg_str = f"{''.join(dead_msg_list)}" if dead_msg_list else "Klidná noc. Dnes nikdo nezemřel."
    
    game_state["phase"] = "Den"
    all_roles_payload = [{"name": p["name"], "role": p["actual_role"], "alive": p["alive"]} for p in game_state["players"].values()]
    
    for sid, p in game_state["players"].items():
        payload = {'msg': msg_str, 'dead': len(actual_deaths) > 0, 'personal_msgs': personal_msgs.get(sid, []), 'is_alive': p["alive"]}
        if not p["alive"]: payload["all_roles"] = all_roles_payload
        if p.get("connected", True):
            emit('day_phase', payload, to=sid)

@socketio.on('start_voting')
def handle_start_voting():
    if game_state["phase"] != "Den": return
    if request.sid != game_state["host_sid"]: return
    game_state["phase"] = "Hlasování"
    game_state["votes"] = {}
    
    alive_players = [p["name"] for p in game_state["players"].values() if p["alive"]]
    all_roles_payload = [{"name": p["name"], "role": p["actual_role"], "alive": p["alive"]} for p in game_state["players"].values()]
    
    for sid, p in game_state["players"].items():
        payload = {'candidates': alive_players}
        if not p["alive"]: payload["all_roles"] = all_roles_payload
        if p.get("connected", True):
            emit('voting_started', payload, to=sid)

@socketio.on('submit_vote')
def handle_submit_vote(data):
    if game_state["phase"] != "Hlasování": return
    sid = request.sid
    if sid not in game_state["players"] or not game_state["players"][sid]["alive"]: return
    
    target = data.get('target')
    voter_name = game_state["players"][sid]["name"]
    if target == voter_name: return
    
    game_state["votes"][sid] = target
    evaluate_votes_if_ready()

def evaluate_votes_if_ready():
    alive_sids = [s for s, p in game_state["players"].items() if p["alive"]]
    connected_alive_sids = [s for s in alive_sids if game_state["players"][s].get("connected", True)]
    
    if not connected_alive_sids: return
    
    votes_submitted = [s for s in game_state["votes"] if s in connected_alive_sids]
    if len(votes_submitted) >= len(connected_alive_sids):
        evaluate_votes()

def evaluate_votes():
    vote_points, vote_details = {}, {}

    for sid, target in game_state["votes"].items():
        player = game_state["players"].get(sid)
        if not player: continue
        voter_name = player["name"]
        vote_points[target] = vote_points.get(target, 0) + 1
        vote_details.setdefault(target, []).append(voter_name)

    if not vote_points: return

    eliminated = max(vote_points, key=vote_points.get)
    
    if eliminated == "__SKIP__":
        res_str = f"<div class='text-2xl font-black text-slate-300 mb-2'><i class='fa-solid fa-person-walking-arrow-right mr-2'></i> Město se rozhodlo <span class='text-white'>přeskočit hlasování</span>. Nikdo nebyl oběšen!</div>"
    else:
        res_str = f"<div class='text-2xl font-black text-white mb-2'>Oběšen byl(a): <span class='text-red-500'>{eliminated}</span></div>"
        if game_state["settings"]["reveal_roles"]:
            p = next((p for p in game_state["players"].values() if p["name"] == eliminated), None)
            if p:
                res_str += f"<div class='text-amber-400 font-bold mb-4'>Ukázalo se, že to byl(a): {p['actual_role']}!</div>"
                
    res_str += "<div class='space-y-2 mt-4 bg-slate-800/50 p-4 rounded-xl'>"
    for tgt, pts in sorted(vote_points.items(), key=lambda x: x[1], reverse=True):
        tgt_name = "Přeskočit hlasování" if tgt == "__SKIP__" else tgt
        if game_state["settings"]["public_voting"]: 
            res_str += f"<div class='text-slate-300 flex justify-between border-b border-slate-700/50 pb-1 last:border-0'><span class='font-bold text-white'>{tgt_name} ({pts} hl.)</span> <span class='text-sm italic text-slate-400'>{', '.join(vote_details[tgt])}</span></div>"
        else: 
            res_str += f"<div class='text-slate-300 border-b border-slate-700/50 pb-1 last:border-0'><span class='font-bold text-white'>{tgt_name}:</span> {pts} hlasů</div>"
    res_str += "</div>"

    if eliminated != "__SKIP__":
        for p in game_state["players"].values():
            if p["name"] == eliminated:
                p["alive"] = False
                if p["actual_role"] == "Šašek":
                    final_msg = res_str + "<br><div class='text-amber-500 text-3xl font-black mt-6 animate-pulse text-center'>🤡 ŠAŠEK BYL UPÁLEN A VYHRÁVÁ HRU! 🤡</div>"
                    emit('game_over', {'winner': 'Šašek', 'msg': final_msg}, broadcast=True)
                    game_state["phase"] = "Lobby"
                    return

    check_win_condition(is_night=False, custom_msg=res_str)

def get_winner():
    alive_mafia = sum(1 for p in game_state["players"].values() if p["actual_role"] == "Mafián" and p["alive"])
    alive_town = sum(1 for p in game_state["players"].values() if p["actual_role"] != "Mafián" and p["alive"])

    if alive_mafia == 0:
        return 'Měšťané', "Všichni zloduchové jsou mrtví!"
    if alive_mafia >= alive_town:
        return 'Mafie', "Mafie přečíslila město a ovládla ho!"
    return None, None

def check_win_condition(is_night=False, custom_msg=""):
    winner, default_msg = get_winner()

    if winner:
        emit('game_over', {'winner': winner, 'msg': custom_msg or default_msg}, broadcast=True)
        game_state["phase"] = "Lobby"
        return True

    if not is_night: emit('trial_results', {'msg': custom_msg}, broadcast=True)
    return False

def get_player_info():
    return [{"name": p["name"], "is_host": (sid == game_state["host_sid"]), "connected": p.get("connected", True)} for sid, p in game_state["players"].items()]

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=debug_mode, host='0.0.0.0', port=port)
