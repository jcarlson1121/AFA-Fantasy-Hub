#!/usr/bin/env python3
"""AFA Fantasy Hub: safely refresh the inline dataset from the public Dynasty Tracker API.

Only replaces <script id="dataset"> content in index.html. No external dependencies.
Can run locally for testing with: python afa_sync.py --source /path/to/raw-export.json
The GitHub Actions workflow runs this unattended and commits changed index.html.
"""
import argparse
import collections
import copy
import datetime as dt
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

API = 'https://www.dynastytracker.app/api/view-dynasty?code=w5g1f7e9'
ROOT = Path(__file__).resolve().parent
HTML = ROOT / 'index.html'
DATA_RE = re.compile(r'(<script\s+id="dataset"\s+type="application/json">)(.*?)(</script>)', re.S)
CAT_ORDER = ('passing', 'rushing', 'receiving', 'blocking', 'defense', 'kicking', 'punting', 'kickReturn', 'puntReturn')
GROUPS = {'QB':'QB','HB':'HB','RB':'HB','FB':'FB','WR':'WR','TE':'TE',
          'LT':'OL','LG':'OL','C':'OL','RG':'OL','RT':'OL','OL':'OL',
          'DT':'DT','LEDG':'EDGE','REDG':'EDGE','LE':'EDGE','RE':'EDGE','EDGE':'EDGE',
          'SAM':'LB','MIKE':'LB','WILL':'LB','MLB':'LB','OLB':'LB','LB':'LB',
          'CB':'CB','FS':'S','SS':'S','S':'S','K':'K','P':'P'}


def number(x):
    if x is None or isinstance(x, bool): return 0.0
    try:
        n = float(str(x).replace(',', ''))
        return n if math.isfinite(n) else 0.0
    except (ValueError, TypeError): return 0.0


def nname(x):
    return re.sub(r'[^a-z0-9]+', '', str(x or '').casefold())


def unwrap(entries):
    return [v['data'] if isinstance(v,dict) and isinstance(v.get('data'),dict) else v
            for v in entries if isinstance(v,dict)]


def source_json(path):
    if path:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    req = urllib.request.Request(API, headers={'Accept':'application/json', 'User-Agent':'AFA-Fantasy-Hub/1.0'})
    with urllib.request.urlopen(req, timeout=90) as resp:
        if resp.status != 200: raise RuntimeError(f'API returned HTTP {resp.status}')
        if int(resp.headers.get('Content-Length','0')) > 100_000_000:
            raise RuntimeError('API response unexpectedly large')
        payload = resp.read(100_000_001)
        if len(payload) > 100_000_000: raise RuntimeError('API response unexpectedly large')
        return json.loads(payload)


def ensure_source(source, old):
    if not isinstance(source,dict) or not isinstance(source.get('mainDoc'),dict):
        raise RuntimeError('Missing mainDoc: refusing to replace website data')
    if not isinstance(source.get('players'),list) or not isinstance(source.get('games'),list):
        raise RuntimeError('Missing player/game collections: refusing update')
    players = unwrap(source['players']); games = unwrap(source['games'])
    # This hub is the 2025 season. Do not mix later dynasty seasons into it.
    games = [g for g in games if str(g.get('year', 2025)) == '2025']
    if len(players) < len(old['players']) or len(players) < 100:
        raise RuntimeError(f'Player count would decrease ({len(old["players"])} -> {len(players)}); leaving website unchanged')
    complete = [g for g in games if isinstance(g.get('boxScore'),dict)
                and isinstance(g['boxScore'].get('byTid'),dict)
                and any(isinstance(v,dict) and any(isinstance(rows,list) and rows for rows in v.values())
                        for v in g['boxScore']['byTid'].values())]
    existing_game_ids = {g['id'] for g in old['games']}
    incoming_game_ids = {g.get('id') for g in complete}
    if not existing_game_ids.issubset(incoming_game_ids):
        raise RuntimeError(f'{len(existing_game_ids - incoming_game_ids)} previously published games missing; refusing destructive update')
    old_player_ids = {p['id'] for p in old['players']}
    incoming_player_ids = {p.get('id') or ('player-'+str(p['pid']) if p.get('pid') is not None else '') for p in players}
    if not old_player_ids.issubset(incoming_player_ids):
                      print("MISSING PLAYER IDS:", sorted(old_player_ids - incoming_player_ids), flush=True)
        raise RuntimeError(f'{len(old_player_ids - incoming_player_ids)} previously published players missing; refusing destructive update')
    if len(complete) < len(old['games']):
        raise RuntimeError(f'Completed game count would decrease ({len(old["games"])} -> {len(complete)}); leaving website unchanged')
    return players, complete


def score(category, st, group, rules):
    if category == 'defense':
        r = rules['defense — CB / S' if group in ('CB','S') else
                  'defense — LB' if group == 'LB' else
                  'defense — DT / EDGE' if group in ('DT','EDGE') else
                  'defense — other positions']
    else: r = rules[category]
    return sum(number(st.get(k)) * number(coeff) for k, coeff in r.items())


def statline(category, st):
    n=lambda k: int(number(st.get(k))) if number(st.get(k)).is_integer() else number(st.get(k))
    if category=='passing': return f'PASS {n("comp")}/{n("attempts")} CMP, {n("yards")} YD, {n("tD")} TD, {n("iNT")} INT'
    if category=='rushing': return f'RUSH {n("carries")} CAR, {n("yards")} YD, {n("tD")} TD'+(f', {n("brokenTackles")} BTK' if n('brokenTackles') else '')
    if category=='receiving': return f'REC {n("receptions")} REC, {n("yards")} YD, {n("tD")} TD'
    if category=='blocking': return f'BLK {n("pancakes")} PAN, {n("sacksAllowed")} SACKS ALLOWED'
    if category=='defense':
        s=f'DEF {n("solo")} SOLO, {n("assists")} AST'
        for key,label in [('tFL','TFL'),('sack','SACK'),('iNT','INT'),('deflections','PD'),('tD','TD'),('fF','FF'),('fR','FR'),('safeties','SAFETY'),('blocks','BLK')]:
            if n(key): s+=f', {n(key)} {label}'
        return s
    if category=='kicking': return f'K {n("fGM")}/{n("fGA")} FG, {n("xPM")}/{n("xPA")} XP'
    if category=='punting': return f'P {n("punts")} PUNT, {n("netYards")} NET YD, {n("in20")} IN20'
    if category in ('kickReturn','puntReturn'): return f'{"KR" if category=="kickReturn" else "PR"} {n("yards")} YD, {n("tD")} TD'
    return ''


def season_stats_from_contributed(games, byname):
    totals=collections.defaultdict(lambda:collections.defaultdict(lambda:collections.defaultdict(float)))
    played=collections.defaultdict(set)
    for g in games:
        contrib=g.get('statsContributed') or {}
        if not isinstance(contrib,dict): continue
        for name,v in contrib.items():
            if not isinstance(v,dict):continue
            # Named stats are disambiguated by box-score team when possible.
            candidates=byname.get(nname(name), [])
            if len(candidates)!=1: continue
            pid=candidates[0]['id']
            if any(isinstance(st,dict) and st for cat,st in v.items() if cat!='_hadStats'):
                played[pid].add(g.get('id'))
            for category,stats in v.items():
                if category=='_hadStats' or not isinstance(stats,dict):continue
                for k,val in stats.items():
                    if isinstance(val,(int,float)) and not isinstance(val,bool) and math.isfinite(val):
                        totals[pid][category][k] += val
    result={}
    for pid,category in totals.items():
        obj={'gamesPlayed':len(played[pid])}
        for k,st in category.items():
            obj[k]={key:int(v) if v.is_integer() else round(v,4) for key,v in st.items()}
        result[pid]=obj
    return result


def construct(source, old):
    raw_players, raw_games=ensure_source(source,old)
    rules=old['meta']['rules']
    team_map={str(t['id']):copy.deepcopy(t) for t in old['teams']}
    for tid,t in (source['mainDoc'].get('teams') or {}).items():
        if not isinstance(t,dict):continue
        tid=str(t.get('tid',tid))
        team=team_map.setdefault(tid,{'id':tid,'name':str(t.get('teamName') or t.get('name') or tid),'abbr':t.get('abbr') or tid,'logo':'','color':'','w':0,'l':0})
        team.update(name=t.get('teamName') or team['name'], abbr=t.get('abbr') or team['abbr'],
                    logo=t.get('logo') or team.get('logo') or '', color=t.get('primaryColor') or team.get('color') or '')
        team['w']=team['l']=0
    old_by_id={p['id']:p for p in old['players']}
    new_players={}
    by_team_name=collections.defaultdict(list);byname=collections.defaultdict(list)
    for p in raw_players:
        pid=p.get('id') or ('player-'+str(p['pid']) if p.get('pid') is not None else None)
        tid=str(p.get('team',''))
        if not pid or not tid or tid not in team_map:continue
        team=team_map[tid]
        name=p.get('name') or f'{p.get("firstName","")} {p.get("lastName","")}'.strip()
        if not name: continue
        pos=str(p.get('position') or '?')
        prev=old_by_id.get(pid,{})
        player={'id':pid,'name':name,'tid':tid,'team':team['name'],'abbr':team['abbr'],
                'pos':pos,'group':GROUPS.get(pos,pos),'ovr':p.get('overall'),'class':p.get('year') or '',
                'dev':p.get('devTrait'),'hype':prev.get('hype'),'gp':0,'points':0,
                'games':[],'news':prev.get('news',[]),'seasonStats':{},
                'photo':p.get('pictureUrl') or '', 'rankPct':None}
        new_players[pid]=player;by_team_name[(tid,nname(name))].append(player);byname[nname(name)].append(player)
    if len(new_players)<len(old_by_id):
        raise RuntimeError(f'Unique, valid players decreased: {len(old_by_id)} -> {len(new_players)}. No update published')
    seen_game_ids=set();out_games=[];matched_rows=0;unmatched_rows=0;duplicate_rows=0;matched_player_game=set()
    one_side=0;two_side=0
    for g in raw_games:
        gid=g.get('id');a=str(g.get('team1Tid') or '');b=str(g.get('team2Tid') or '')
        if not gid or not a or not b or a not in team_map or b not in team_map or gid in seen_game_ids:
            raise RuntimeError('Bad or duplicate completed-game ID/team. Refusing update')
        seen_game_ids.add(gid)
        if not all(isinstance(g.get(z),(int,float)) for z in ('team1Score','team2Score')):
            raise RuntimeError(f'Game {gid} has missing final score')
        bs=g['boxScore'];boxes=bs.get('byTid') or {}
        # Derive the coverage per actual team, not by empty stat category shells.
        cov={};box_out={};fantasy={};notes=[]
        for tid in (a,b):
            raw=boxes.get(tid) or {}
            filled=bool(raw and any(isinstance(v,list) and v for v in raw.values()))
            cov[tid]='complete' if filled else 'missing'
            if not filled:
                box_out[tid]=[];fantasy[tid]=[]
                notes.append(f'No player box score recorded for {team_map[tid]["name"]}.')
                continue
            rows=[];total_by_player={}
            for category in CAT_ORDER:
                for st in raw.get(category,[]) or []:
                    if not isinstance(st,dict):continue
                    name=st.get('playerName','')
                    if not str(name).strip():
                        unmatched_rows+=1;continue
                    values={k:v for k,v in st.items() if k!='playerName' and v is not None}
                    if not values:continue
                    row={'name':name,'category':category,'stats':values}
                    if row in rows:
                        duplicate_rows+=1;continue
                    rows.append(row)
                    candidates=by_team_name.get((tid,nname(name)),[])
                    if len(candidates)!=1:
                        unmatched_rows+=1;continue
                    p=candidates[0]; pid=p['id'];matched_rows+=1
                    e=total_by_player.setdefault(pid,{'player':p,'parts':{},'lines':[]})
                    pts=score(category,values,p['group'],rules)
                    e['parts'][category]=e['parts'].get(category,0)+pts
                    e['lines'].append(statline(category,values))
            box_out[tid]=rows
            fantasy_side=[]
            for pid,e in total_by_player.items():
                p=e['player'];pts=sum(e['parts'].values());opid=b if tid==a else a
                game_rec={'week':g.get('week',0),'opponent':team_map[opid]['abbr'],
                          'score':round(pts,2),'won':(number(g['team1Score'])>number(g['team2Score'])) if tid==a else (number(g['team2Score'])>number(g['team1Score'])),
                          'parts':{k:round(v,5) for k,v in e['parts'].items()},'gameId':gid,'statLine':' · '.join(e['lines'])}
                p['games'].append(game_rec);p['points']+=pts; matched_player_game.add((pid,gid))
                fantasy_side.append({'group':p['group'],'id':pid,'name':p['name'],'ovr':p['ovr'],
                                     'parts':game_rec['parts'],'points':round(pts,2),'pos':p['pos'], 'statLine':game_rec['statLine']})
            fantasy[tid]=sorted(fantasy_side,key=lambda x:-x['points'])
        count=sum(x=='complete' for x in cov.values())
        if count==1:one_side+=1
        if count==2:two_side+=1
        oldg=next((x for x in old['games'] if x['id']==gid),None)
        out_games.append({'id':gid,'week':g.get('week',0),'a':a,'b':b,'sa':g['team1Score'],'sb':g['team2Score'],
                          'rankA':g.get('team1Rank'),'rankB':g.get('team2Rank'),
                          'recap':g.get('aiRecap') or (oldg or {}).get('recap',''),
                          'quarters':g.get('quarters') or {},'fantasy':fantasy,'box':box_out,
                          'scoringSummary':bs.get('scoringSummary') or [],
                          'teamStats':bs.get('teamStatsByTid') or {}, 'coverage':cov,'qualityNotes':notes})
        if number(g['team1Score'])>number(g['team2Score']):team_map[a]['w']+=1;team_map[b]['l']+=1
        elif number(g['team2Score'])>number(g['team1Score']):team_map[b]['w']+=1;team_map[a]['l']+=1
    if unmatched_rows > max(5, matched_rows * 0.02):
        raise RuntimeError(f'Unmatched player stat rows too high ({unmatched_rows}); refusing update')
    # No unlogged removal of recorded games.
    if len(out_games)<len(old['games']):raise RuntimeError('Game count shrank after validation; refusing update')
    per_group=collections.defaultdict(list)
    for p in new_players.values():
        p['games'].sort(key=lambda game:(game['week'],game['gameId']))
        p['gp']=len(p['games']);p['points']=round(p['points'],2)
        if p['gp']:per_group[p['group']].append(p['points']/p['gp'])
    for values in per_group.values(): values.sort()
    import bisect
    for p in new_players.values():
        if p['gp']:
            vals=per_group[p['group']];avg=p['points']/p['gp']
            # Midrank percentile (ties at average rank), comparable across a position group.
            p['rankPct']=round((bisect.bisect_left(vals,avg)+bisect.bisect_right(vals,avg))/2/max(len(vals),1)*100,1)
    contributed=season_stats_from_contributed(raw_games,byname)
    for pid,p in new_players.items():
        p['seasonStats']=contributed.get(pid, {'gamesPlayed':p['gp']} if p['gp'] else {})
    out={
        'teams':sorted(team_map.values(),key=lambda t:int(t['id']) if t['id'].isdigit() else t['id']),
        'players':sorted(new_players.values(),key=lambda p:p['id']),
        'meta':copy.deepcopy(old['meta']),
        'games':sorted(out_games,key=lambda g:(number(g['week']),g['id']))}
    out['meta'].update(games=len(out_games),matched=len(matched_player_game),unmatched=unmatched_rows)
    out['meta']['audit']={'completeGames':len(out_games),'distinctMatchedPlayerGames':len(matched_player_game),
                          'matchedBoxRows':matched_rows,'namedRowsUnmatched':unmatched_rows,
                          'unnamedStatRowsExcludedFromFantasy':0,'duplicateIdenticalRowsRemoved':duplicate_rows,
                          'oneTeamBoxScores':one_side,'twoTeamBoxScores':two_side}
    out['meta']['scoringExplanation']=(f'Stats come from {len(out_games)} completed-game box scores. '
                                       'Fantasy points are awarded only for matched, recorded events; '
                                       f'{one_side} games have box scores for only one team. Missing data is not treated as zero.')
    return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',help='Optional raw JSON file for offline testing')
    ap.add_argument('--output', help='Optional output HTML file (default: update index.html)')
    args=ap.parse_args()
    text=HTML.read_text(encoding='utf-8')
    m=DATA_RE.search(text)
    if not m:raise RuntimeError('Cannot locate Fantasy Hub dataset in index.html')
    old=json.loads(m.group(2));raw=source_json(args.source)
    new=construct(raw,old)
    payload=json.dumps(new,ensure_ascii=False,separators=(',',':')).replace('</','<\\/')
    updated=text[:m.start(2)]+payload+text[m.end(2):]
    updated=updated.replace('Static data snapshot · Not automatically updated',
                            'Refreshed from Dynasty Tracker when new data is available')
    target=Path(args.output) if args.output else HTML
    if target.exists() and target.read_text(encoding='utf-8') == updated:
        print('NO CHANGE: website data and HTML already match.');return
    tmp=target.with_suffix(target.suffix+'.tmp')
    tmp.write_text(updated,encoding='utf-8');tmp.replace(target)
    print(f'UPDATED {target}: {len(new["teams"])} teams, {len(new["players"])} players, {len(new["games"])} games')
    print('Audit:',json.dumps(new['meta']['audit']))

if __name__=='__main__':
    try: main()
    except Exception as e:
        print(f'ABORTED: {e}',file=sys.stderr)
        sys.exit(1)
