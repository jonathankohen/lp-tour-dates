/*
 * formats.js — copy-paste text builders.
 *
 * These are JavaScript ports of the text produced by the Python job's
 * outputs/doc.py. Treat doc.py as the source of truth: if the Doc format
 * changes there, mirror it here.
 *
 *   emailDates  ← _build_email_text         (booked only, month headers)
 *   zoneLists   ← EMAIL_ZONES grouping       (booked only, grouped by region)
 *   openDates   ← _assemble_doc_sections     (season/month groups + OPEN fill-ins)
 *   simpleList  ←  plain MM/DD/YY lines
 *
 * Exposed as window.TourFormats.
 */
(function () {
	'use strict';

	var WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
	var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
		'July', 'August', 'September', 'October', 'November', 'December'];

	// Geographic zones — mirrors EMAIL_ZONES in outputs/doc.py.
	var EMAIL_ZONES = [
		['New England', ['CT', 'MA', 'ME', 'NH', 'RI', 'VT']],
		['Mid-Atlantic', ['DC', 'DE', 'MD', 'NJ', 'NY', 'PA']],
		['Southeast', ['AL', 'FL', 'GA', 'MS', 'NC', 'SC', 'TN', 'VA', 'WV']],
		['South Central', ['AR', 'KY', 'LA', 'MO', 'OK', 'TX']],
		['Great Lakes', ['IL', 'IN', 'MI', 'OH', 'WI']],
		['Plains', ['IA', 'KS', 'MN', 'NE', 'ND', 'SD']],
		['Mountain', ['CO', 'ID', 'MT', 'NM', 'UT', 'WY']],
		['Southwest', ['AZ', 'CA', 'NV']],
		['Pacific Northwest', ['OR', 'WA']]
	];

	/* ---- date helpers (timezone-safe: parse Y-M-D as local, no UTC shift) ---- */

	function parseDate(str) {
		var p = String(str).split('-');
		return new Date(+p[0], (+p[1]) - 1, +p[2]);
	}
	function pad2(n) { return n < 10 ? '0' + n : '' + n; }
	// "Friday, July 10"  (Python %A, %B %-d)
	function fmtLong(d) {
		return WEEKDAYS[d.getDay()] + ', ' + MONTHS[d.getMonth()] + ' ' + d.getDate();
	}
	// "July 2026"
	function fmtMonthYear(d) { return MONTHS[d.getMonth()] + ' ' + d.getFullYear(); }
	// "07/10/26"
	function fmtMMDDYY(d) {
		return pad2(d.getMonth() + 1) + '/' + pad2(d.getDate()) + '/' + String(d.getFullYear()).slice(-2);
	}
	function monthKey(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1); }

	// (sortKey, label) — mirrors _season_key in doc.py.
	function seasonInfo(d) {
		var m = d.getMonth() + 1, y = d.getFullYear();
		if (m >= 3 && m <= 5) { return [y + '-0', 'Spring ' + y]; }
		if (m >= 6 && m <= 8) { return [y + '-1', 'Summer ' + y]; }
		if (m >= 9 && m <= 11) { return [y + '-2', 'Fall ' + y]; }
		var sy = m === 12 ? y : y - 1;
		return [sy + '-3', 'Winter ' + sy];
	}

	function locationOf(show) {
		var parts = [show.city, show.region].filter(function (p) { return p; });
		return parts.join(', ');
	}

	function bySortedDate(shows) {
		return shows.slice().sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
	}

	function groupByArtist(shows) {
		var map = {};
		shows.forEach(function (s) { (map[s.artist] = map[s.artist] || []).push(s); });
		return Object.keys(map).sort().map(function (a) { return [a, map[a]]; });
	}

	/* ---- 1. Per-artist email dates (booked only, month headers) ---- */

	function emailBlock(shows) {
		var sorted = bySortedDate(shows);
		var out = [];
		var current = null;
		sorted.forEach(function (s) {
			var d = parseDate(s.date);
			var mk = monthKey(d);
			if (mk !== current) {
				if (out.length) { out.push(''); }
				out.push(fmtMonthYear(d));
				out.push('');
				current = mk;
			}
			out.push(fmtLong(d));
			out.push(locationOf(s) || s.venue || '');
			out.push('');
		});
		// drop trailing blank
		while (out.length && out[out.length - 1] === '') { out.pop(); }
		return out.join('\n');
	}

	function emailDates(shows) {
		return groupByArtist(shows).map(function (pair) {
			return pair[0] + '\n\n' + emailBlock(pair[1]);
		}).join('\n\n\n');
	}

	/* ---- 2. Geographic zone lists (booked only, grouped by region) ---- */

	function zoneLists(shows) {
		return groupByArtist(shows).map(function (pair) {
			var artist = pair[0], aShows = pair[1];
			var blocks = [];
			EMAIL_ZONES.forEach(function (z) {
				var name = z[0], states = z[1];
				var zShows = aShows.filter(function (s) { return states.indexOf(s.region) !== -1; });
				if (!zShows.length) { return; }
				var present = uniqueSorted(zShows.map(function (s) { return s.region; }).filter(Boolean));
				blocks.push(name + ' — States: ' + present.join(', ') + '\n\n' + emailBlock(zShows));
			});
			// Shows with no recognized US region.
			var other = aShows.filter(function (s) { return !inAnyZone(s.region); });
			if (other.length) {
				blocks.push('Other\n\n' + emailBlock(other));
			}
			return artist + '\n\n' + blocks.join('\n\n\n');
		}).join('\n\n\n');
	}

	function inAnyZone(region) {
		return EMAIL_ZONES.some(function (z) { return z[1].indexOf(region) !== -1; });
	}
	function uniqueSorted(arr) {
		return arr.filter(function (v, i, a) { return a.indexOf(v) === i; }).sort();
	}

	/* ---- 3. Open dates / routing (season + month groups, OPEN fill-ins) ---- */

	// Mirror of _build_doc_month_text: input shows are all in one calendar month.
	function monthRouting(monthShows) {
		var first = parseDate(monthShows[0].date);
		var y = first.getFullYear(), m = first.getMonth();
		var byTime = {};            // time -> show (or null for OPEN)
		monthShows.forEach(function (s) {
			var d = parseDate(s.date);
			byTime[d.getTime()] = s;
			for (var i = 1; i <= 2; i++) {
				[-i, i].forEach(function (delta) {
					var od = new Date(y, m, d.getDate() + delta);
					if (od.getFullYear() === y && od.getMonth() === m && !(od.getTime() in byTime)) {
						byTime[od.getTime()] = null;
					}
				});
			}
		});
		return Object.keys(byTime).map(Number).sort(function (a, b) { return a - b; }).map(function (t) {
			var d = new Date(t);
			var show = byTime[t];
			if (show === null) { return fmtLong(d) + ' - OPEN'; }
			var loc = locationOf(show);
			var venue = show.venue || '';
			if (venue && loc) { return fmtLong(d) + ' - ' + venue + ', ' + loc; }
			if (venue) { return fmtLong(d) + ' - ' + venue; }
			return fmtLong(d) + ' - ' + loc;
		}).join('\n');
	}

	function routingBlock(shows) {
		// group by season, then by month within season
		var seasons = {}; var labels = {};
		shows.forEach(function (s) {
			var info = seasonInfo(parseDate(s.date));
			(seasons[info[0]] = seasons[info[0]] || []).push(s);
			labels[info[0]] = info[1];
		});
		var sectionTexts = Object.keys(seasons).sort().map(function (sk) {
			var months = {};
			seasons[sk].forEach(function (s) { (months[s.date.slice(0, 7)] = months[s.date.slice(0, 7)] || []).push(s); });
			var body = Object.keys(months).sort().map(function (mk) { return monthRouting(months[mk]); }).join('\n');
			return labels[sk] + '\n' + body;
		});
		var states = uniqueSorted(shows.map(function (s) { return s.region; }).filter(Boolean));
		var text = sectionTexts.join('\n\n');
		if (states.length) { text += '\n\nStates: ' + states.join(', '); }
		return text;
	}

	function openDates(shows) {
		return groupByArtist(shows).map(function (pair) {
			return pair[0] + '\n\n' + routingBlock(pair[1]);
		}).join('\n\n\n');
	}

	/* ---- 4. Simple list ---- */

	function simpleList(shows, opts) {
		opts = opts || {};
		var sorted = bySortedDate(shows);
		var multiArtist = uniqueSorted(shows.map(function (s) { return s.artist; })).length > 1;
		return sorted.map(function (s) {
			var d = parseDate(s.date);
			var loc = locationOf(s);
			var line = fmtMMDDYY(d) + ' — ';
			if (multiArtist) { line += s.artist + ' — '; }
			line += [s.venue, loc].filter(Boolean).join(', ');
			if (opts.withLinks && s.ticket_url) { line += ' — ' + s.ticket_url; }
			return line;
		}).join('\n');
	}

	window.TourFormats = {
		EMAIL_ZONES: EMAIL_ZONES,
		parseDate: parseDate,
		fmtLong: fmtLong,
		fmtMonthYear: fmtMonthYear,
		fmtMMDDYY: fmtMMDDYY,
		seasonInfo: seasonInfo,
		locationOf: locationOf,
		emailDates: emailDates,
		zoneLists: zoneLists,
		openDates: openDates,
		simpleList: simpleList
	};
})();
