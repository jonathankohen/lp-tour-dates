/**
 * Query parsing + filtering for the read-only /shows endpoint — a line-for-line
 * port of wordpress-plugin/tour-calendar/includes/read-filters.php.
 *
 * Everything in this file is PURE: plain objects in, plain objects out, no
 * bindings, no I/O. test/filters.test.js exercises it with `node --test` alone.
 *
 * The contract (query names, error strings, sort order, limit caps) must stay
 * identical to the PHP so a consumer can switch base URLs and notice nothing.
 * When one side changes, change the other and its tests in the same commit.
 */

export const READ_LIMIT_DEFAULT = 1000;
export const READ_LIMIT_MAX = 5000;

const encoder = new TextEncoder();

/**
 * Constant-time string equality. Compares every byte regardless of where the
 * first mismatch is, and a length mismatch still walks the longer string, so a
 * wrong guess costs the same as a near miss.
 */
export function timingSafeEqual(a, b) {
  const ab = encoder.encode(String(a));
  const bb = encoder.encode(String(b));
  const len = Math.max(ab.length, bb.length);
  let diff = ab.length ^ bb.length;
  for (let i = 0; i < len; i++) {
    diff |= (ab[i] ?? 0) ^ (bb[i] ?? 0);
  }
  return diff === 0;
}

/**
 * Decide whether a presented credential may read the shows feed.
 *
 * Accepts the read key, or the ingest secret (a strict superset, so the team's
 * own tooling needs no second credential). BOTH candidates are always evaluated
 * so a rejection costs the same either way. An empty configured credential can
 * never be matched by an empty presented one — without the emptiness guards an
 * unconfigured deployment would accept a request that sent nothing.
 */
export function readCredentialOk(readKey, secret, providedKey, providedSecret) {
  readKey = String(readKey ?? '');
  secret = String(secret ?? '');
  providedKey = String(providedKey ?? '');
  providedSecret = String(providedSecret ?? '');
  const byKey = readKey !== '' && providedKey !== '' && timingSafeEqual(readKey, providedKey);
  const bySecret = secret !== '' && providedSecret !== '' && timingSafeEqual(secret, providedSecret);
  return byKey || bySecret;
}

/**
 * Pull a bearer token out of an Authorization header. Returns '' when the header
 * is absent or is not a Bearer credential, so "Basic …" is never mistaken for a key.
 */
export function bearerToken(authorization) {
  const value = String(authorization ?? '');
  if (!value.toLowerCase().startsWith('bearer ')) {
    return '';
  }
  return value.slice(7).trim();
}

/** Normalize a name to lowercase alphanumerics: "Bohemian-Queen" → "bohemianqueen". */
export function readNorm(value) {
  return String(value ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

/**
 * Strict YYYY-MM-DD check, including that the date actually exists.
 * Rejects "2026-13-01" and "2026-02-30", which a bare regex would pass.
 */
export function isYmd(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value ?? ''));
  if (!m) {
    return false;
  }
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  if (mo < 1 || mo > 12 || d < 1) {
    return false;
  }
  // Day 0 of the next month is the last day of this one.
  const daysInMonth = new Date(Date.UTC(y, mo, 0)).getUTCDate();
  return d <= daysInMonth;
}

/** PHP's (int) cast on a string: leading integer, else 0. */
function phpInt(value) {
  const m = /^\s*([+-]?\d+)/.exec(String(value));
  return m ? Number(m[1]) : 0;
}

function str(value) {
  return value === undefined || value === null ? '' : String(value);
}

/**
 * Turn raw query parameters into a normalized argument set.
 *
 * Returns { error: 'message' } on a malformed value. A bad date is an error
 * rather than a silently-ignored filter: a bot that asks for one month and is
 * handed the whole roster would not notice, and would act on it.
 *
 * @param {Record<string, string>} query Raw query params, as strings.
 */
export function parseReadArgs(query) {
  query = query ?? {};
  const args = {
    artists: [],
    regions: [],
    from: '',
    to: '',
    q: '',
    limit: READ_LIMIT_DEFAULT,
    offset: 0,
  };

  if (query.artist !== undefined && str(query.artist).trim() !== '') {
    for (const name of str(query.artist).split(',')) {
      const norm = readNorm(name);
      if (norm !== '') {
        args.artists.push(norm);
      }
    }
  }

  if (query.region !== undefined && str(query.region).trim() !== '') {
    for (const raw of str(query.region).split(',')) {
      const region = raw.trim().toUpperCase();
      if (region !== '') {
        args.regions.push(region);
      }
    }
  }

  for (const bound of ['from', 'to']) {
    if (query[bound] !== undefined && str(query[bound]).trim() !== '') {
      const value = str(query[bound]).trim();
      if (!isYmd(value)) {
        return { error: `Invalid "${bound}" date "${value}" — expected YYYY-MM-DD.` };
      }
      args[bound] = value;
    }
  }

  if (args.from !== '' && args.to !== '' && args.from > args.to) {
    return { error: 'Invalid date range — "from" is after "to".' };
  }

  if (query.q !== undefined) {
    args.q = str(query.q).trim().toLowerCase();
  }

  if (query.limit !== undefined && str(query.limit).trim() !== '') {
    const limit = phpInt(query.limit);
    if (limit < 1) {
      return { error: '"limit" must be a positive integer.' };
    }
    args.limit = Math.min(limit, READ_LIMIT_MAX);
  }

  if (query.offset !== undefined && str(query.offset).trim() !== '') {
    const offset = phpInt(query.offset);
    if (offset < 0) {
      return { error: '"offset" must be zero or greater.' };
    }
    args.offset = offset;
  }

  return args;
}

function isRow(show) {
  return show !== null && typeof show === 'object' && !Array.isArray(show);
}

/** Byte-order string compare, matching PHP strcmp for the ASCII data we store. */
function cmp(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Apply the parsed filters to the stored show rows.
 *
 * Filters are ANDed. Results are sorted by date, then artist, then venue — a
 * stable order matters because ?offset= paging over an unsorted list can repeat
 * or skip rows between calls.
 *
 * @returns {{ total: number, shows: object[] }} total counts matches BEFORE
 *   limit/offset, so a caller can tell there is more.
 */
export function filterShows(shows, args) {
  const matched = [];

  for (const show of Array.isArray(shows) ? shows : []) {
    if (!isRow(show)) {
      continue;
    }
    const date = str(show.date);

    if (args.artists.length) {
      if (!args.artists.includes(readNorm(show.artist))) {
        continue;
      }
    }

    if (args.regions.length) {
      const region = str(show.region).trim().toUpperCase();
      if (!args.regions.includes(region)) {
        continue;
      }
    }

    if (args.from !== '' || args.to !== '') {
      // An undated or malformed row cannot be placed in a range, so a range
      // query excludes it rather than guessing.
      if (!isYmd(date)) {
        continue;
      }
      if (args.from !== '' && date < args.from) {
        continue;
      }
      if (args.to !== '' && date > args.to) {
        continue;
      }
    }

    if (args.q !== '') {
      const haystack = `${str(show.artist)} ${str(show.venue)} ${str(show.city)}`.toLowerCase();
      if (!haystack.includes(args.q)) {
        continue;
      }
    }

    matched.push(show);
  }

  matched.sort((a, b) =>
    cmp(str(a.date), str(b.date)) ||
    cmp(str(a.artist), str(b.artist)) ||
    cmp(str(a.venue), str(b.venue)));

  return {
    total: matched.length,
    shows: matched.slice(args.offset, args.offset + args.limit),
  };
}
