// Device mirror of agent/location_phrases.py: «когда приду домой, напомни разобрать вещи».
// The place is matched against the user's own places; otherwise the user picks one.
const L = '(?<![0-9a-zа-я])';
const R = '(?![0-9a-zа-я])';
const PATTERNS = [
  [`${L}когда\\s+(?:я\\s+)?(?:приду|вернусь|буду|окажусь|доберусь|приеду|зайду)\\s+(?:домой|(?:в|во|на|к|у)\\s+(?<place>[^,.!?]+?))${R}(?=\\s*(?:,|напомн|скажи|$))`, 'ENTER'],
  [`${L}когда\\s+(?:я\\s+)?(?:уйду|выйду|уеду|выеду)\\s+(?:из|с|со|от)\\s+(?<place>[^,.!?]+?)${R}(?=\\s*(?:,|напомн|скажи|$))`, 'EXIT'],
  [`${L}when\\s+i\\s+(?:get|arrive|am|come)\\s+(?:home|(?:to|at|back\\s+to|in)\\s+(?:the\\s+)?(?<place>[^,.!?]+?))${R}(?=\\s*(?:,|remind|$))`, 'ENTER'],
  [`${L}when\\s+i\\s+leave\\s+(?:the\\s+)?(?<place>[^,.!?]+?)${R}(?=\\s*(?:,|remind|$))`, 'EXIT'],
];
const HOME_WORDS = new Set(['домой', 'home']);
const HOME_NAMES = ['дом', 'home'];
const REMIND = `${L}(?:напомни(?:те)?|напоминай|скажи|remind\\s+me(?:\\s+to)?|remind)${R}(?:\\s+(?:мне|me|to|о|про))?`;
const low = (text) => String(text || '').toLowerCase().replaceAll('ё', 'е');
const words = (text) => low(text).match(/[0-9a-zа-я]+/gu) || [];

function stemMatch(a, b) {
  if (a === b) return true;
  const shortest = Math.min(a.length, b.length);
  if (shortest < 3) return false;
  let common = 0;
  while (common < shortest && a[common] === b[common]) common += 1;
  return common >= Math.max(3, shortest - 2);
}

export function matchPlace(text, places) {
  const said = words(text);
  if (!said.length) return [];
  return places.filter((place) => [place.alias, place.display_name, place.name].some((name) => {
    const tokens = words(name);
    return tokens.length && tokens.every((t) => said.some((s) => stemMatch(t, s)));
  }));
}

export function parseLocationTrigger(text, places = []) {
  const raw = String(text || '').split(/\s+/u).filter(Boolean).join(' ');
  if (!raw || raw.length > 300) return null;
  const lowered = low(raw);
  if (lowered.length !== raw.length) return null;
  for (const [pattern, transition] of PATTERNS) {
    const hit = new RegExp(pattern).exec(lowered);
    if (!hit) continue;
    let placeWords = (hit.groups?.place || '').trim();
    let candidates;
    if (!placeWords || HOME_WORDS.has(placeWords)) {
      candidates = places.filter((p) => ['alias', 'display_name', 'name'].some((k) => HOME_NAMES.includes(low(p[k] || ''))));
      placeWords = /[а-я]/u.test(lowered) ? 'дом' : 'home';
    } else {
      candidates = matchPlace(placeWords, places);
    }
    let rest = `${raw.slice(0, hit.index)} ${raw.slice(hit.index + hit[0].length)}`;
    const remind = new RegExp(REMIND).exec(low(rest));
    if (remind) rest = `${rest.slice(0, remind.index)} ${rest.slice(remind.index + remind[0].length)}`;
    let title = rest.replace(/\s+/gu, ' ').replace(/^[ ,.;:—\-!]+|[ ,.;:—\-!]+$/gu, '');
    title = title.replace(/^(?:и|and|что|to)\s+/iu, '').replace(/^[ ,.;:—\-!]+|[ ,.;:—\-!]+$/gu, '');
    if (!title) return null;
    const result = { kind: 'LOCATION_TRIGGER', transition, title: title.slice(0, 1).toUpperCase() + title.slice(1), place_text: placeWords };
    if (candidates.length === 1) { result.place_id = candidates[0].id; result.unresolved = []; } else result.unresolved = ['place_id'];
    return result;
  }
  return null;
}

const ADD_PLACE = /^(?:добавь|создай|сохрани|запомни)\s+(?:новое\s+)?(?:место|адрес)\s+(?<name>[^,:]+?)(?:\s*[,:]\s*(?:адрес\s+)?(?<address>.+))?$|^(?:add|save|create)\s+(?:a\s+)?place\s+(?<en_name>[^,:]+?)(?:\s*[,:]\s*(?:address\s+)?(?<en_address>.+))?$/u;

// «Добавь место Спортзал» → CREATE_PLACE with the user's own words.
export function parsePlaceCreate(text) {
  const raw = String(text || '').split(/\s+/u).filter(Boolean).join(' ').replace(/^[ .!]+|[ .!]+$/gu, '');
  const lowered = low(raw);
  if (!raw || raw.length > 300 || lowered.length !== raw.length) return null;
  const hit = ADD_PLACE.exec(lowered);
  if (!hit) return null;
  const key = hit.groups.name ? 'name' : 'en_name';
  const start = lowered.indexOf(hit.groups[key], lowered.search(/\s(?:место|адрес|place)\s/u));
  const name = raw.slice(start, start + hit.groups[key].length).trim();
  if (!name) return null;
  const result = { display_name: name.slice(0, 1).toUpperCase() + name.slice(1) };
  const addressKey = key === 'name' ? 'address' : 'en_address';
  if (hit.groups[addressKey]) {
    const at = lowered.lastIndexOf(hit.groups[addressKey]);
    result.address = raw.slice(at, at + hit.groups[addressKey].length).trim();
  }
  return result;
}
