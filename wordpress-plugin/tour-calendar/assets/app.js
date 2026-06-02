/*
 * app.js — Tour Calendar front-end (vanilla JS, no dependencies).
 *
 * Reads the inline <script class="tcal-data"> JSON printed by the shortcode and
 * renders: artist toggles, a Calendar/List view switch, filters, and a copy
 * panel that emits the four formats from formats.js. Re-renders the whole UI on
 * state change — the dataset is small (hundreds of shows), so this stays simple.
 */
(function () {
	'use strict';

	var F = window.TourFormats;
	var WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

	function ready(fn) {
		if (document.readyState !== 'loading') { fn(); }
		else { document.addEventListener('DOMContentLoaded', fn); }
	}

	ready(function () {
		var roots = document.querySelectorAll('.tcal-root');
		Array.prototype.forEach.call(roots, initRoot);
	});

	function initRoot(root) {
		var dataEl = root.querySelector('.tcal-data');
		var mount = root.querySelector('.tcal-mount');
		if (!dataEl || !mount) { return; }

		var payload;
		try { payload = JSON.parse(dataEl.textContent || '{}'); }
		catch (e) { payload = { shows: [] }; }

		var shows = (payload.shows || []).filter(function (s) { return s.date; });
		var artists = uniqueSorted(shows.map(function (s) { return s.artist; }));
		var regions = uniqueSorted(shows.map(function (s) { return s.region; }).filter(Boolean));

		// Default the calendar to the month of the earliest upcoming show.
		var firstDate = shows.length ? F.parseDate(shows.map(function (s) { return s.date; }).sort()[0]) : new Date();

		var state = {
			selected: {},                 // artist -> true
			view: 'calendar',
			cursor: new Date(firstDate.getFullYear(), firstDate.getMonth(), 1),
			sort: { key: 'date', dir: 1 },
			search: '',
			region: ''
		};
		artists.forEach(function (a) { state.selected[a] = true; });

		function selectedArtists() {
			return artists.filter(function (a) { return state.selected[a]; });
		}

		function filtered() {
			var q = state.search.trim().toLowerCase();
			return shows.filter(function (s) {
				if (!state.selected[s.artist]) { return false; }
				if (state.region && s.region !== state.region) { return false; }
				if (q) {
					var hay = (s.artist + ' ' + s.venue + ' ' + s.city + ' ' + s.region).toLowerCase();
					if (hay.indexOf(q) === -1) { return false; }
				}
				return true;
			});
		}

		/* ---------------------------------------------------------------- */
		/*  Render                                                          */
		/* ---------------------------------------------------------------- */

		function render() {
			mount.innerHTML = '';
			mount.appendChild(renderBar());
			mount.appendChild(renderArtists());
			var main = el('div', 'tcal-main');
			main.appendChild(state.view === 'calendar' ? renderCalendar() : renderList());
			mount.appendChild(main);
			mount.appendChild(renderCopy());
		}

		function renderBar() {
			var bar = el('div', 'tcal-bar');

			var views = el('div', 'tcal-views');
			views.appendChild(viewBtn('calendar', 'Calendar'));
			views.appendChild(viewBtn('list', 'List'));
			bar.appendChild(views);

			var search = document.createElement('input');
			search.type = 'search';
			search.className = 'tcal-search';
			search.placeholder = 'Filter venue, city, artist…';
			search.value = state.search;
			search.addEventListener('input', function () { state.search = search.value; renderMainOnly(); });
			bar.appendChild(search);

			if (regions.length) {
				var sel = document.createElement('select');
				sel.className = 'tcal-region';
				sel.appendChild(opt('', 'All regions'));
				regions.forEach(function (r) { sel.appendChild(opt(r, r)); });
				sel.value = state.region;
				sel.addEventListener('change', function () { state.region = sel.value; renderMainOnly(); });
				bar.appendChild(sel);
			}

			if (payload.generated_at) {
				var upd = el('span', 'tcal-updated', 'Updated ' + formatUpdated(payload.generated_at));
				bar.appendChild(upd);
			}
			return bar;
		}

		function viewBtn(view, label) {
			var b = el('button', 'tcal-viewbtn' + (state.view === view ? ' is-active' : ''), label);
			b.type = 'button';
			b.addEventListener('click', function () { state.view = view; render(); });
			return b;
		}

		function renderArtists() {
			var wrap = el('div', 'tcal-artists');
			var tools = el('div', 'tcal-artist-tools');
			tools.appendChild(linkBtn('All', function () {
				artists.forEach(function (a) { state.selected[a] = true; }); render();
			}));
			tools.appendChild(linkBtn('None', function () {
				artists.forEach(function (a) { state.selected[a] = false; }); render();
			}));
			wrap.appendChild(tools);

			var chips = el('div', 'tcal-chips');
			artists.forEach(function (a) {
				var on = state.selected[a];
				var chip = el('button', 'tcal-chip' + (on ? ' is-on' : ''), a);
				chip.type = 'button';
				chip.addEventListener('click', function () { state.selected[a] = !state.selected[a]; render(); });
				chips.appendChild(chip);
			});
			wrap.appendChild(chips);
			return wrap;
		}

		function renderMainOnly() {
			var main = mount.querySelector('.tcal-main');
			var copy = mount.querySelector('.tcal-copy');
			if (main) { main.innerHTML = ''; main.appendChild(state.view === 'calendar' ? renderCalendar() : renderList()); }
			if (copy) { mount.replaceChild(renderCopy(), copy); }
		}

		/* ---- Calendar ---- */

		function renderCalendar() {
			var wrap = el('div', 'tcal-calendar');

			var nav = el('div', 'tcal-cal-nav');
			nav.appendChild(navBtn('‹', -1));
			nav.appendChild(el('span', 'tcal-cal-label', F.fmtMonthYear(state.cursor)));
			nav.appendChild(navBtn('›', 1));
			var today = el('button', 'tcal-cal-today', 'This month');
			today.type = 'button';
			today.addEventListener('click', function () {
				var n = new Date();
				state.cursor = new Date(n.getFullYear(), n.getMonth(), 1);
				renderMainOnly();
			});
			nav.appendChild(today);
			wrap.appendChild(nav);

			var visible = filtered();
			var byDay = {};
			visible.forEach(function (s) { (byDay[s.date] = byDay[s.date] || []).push(s); });

			// OPEN fill-in days only make sense for a single artist.
			var openDays = computeOpenDays(visible);

			var grid = el('div', 'tcal-grid');
			WEEKDAY_SHORT.forEach(function (w) { grid.appendChild(el('div', 'tcal-dow', w)); });

			var year = state.cursor.getFullYear(), month = state.cursor.getMonth();
			var firstDow = new Date(year, month, 1).getDay();
			var daysInMonth = new Date(year, month + 1, 0).getDate();

			for (var i = 0; i < firstDow; i++) { grid.appendChild(el('div', 'tcal-cell is-empty')); }

			for (var day = 1; day <= daysInMonth; day++) {
				var key = year + '-' + pad2(month + 1) + '-' + pad2(day);
				var dayShows = byDay[key] || [];
				var cls = 'tcal-cell';
				if (dayShows.length) { cls += ' has-shows'; }
				else if (openDays[key]) { cls += ' is-open'; }
				var cell = el('div', cls);
				cell.appendChild(el('div', 'tcal-cell-day', String(day)));
				dayShows.slice(0, 3).forEach(function (s) {
					var label = (selectedArtists().length > 1 ? s.artist + ' — ' : '') + (s.city || s.venue || '');
					cell.appendChild(el('div', 'tcal-event', label));
				});
				if (dayShows.length > 3) { cell.appendChild(el('div', 'tcal-event tcal-more', '+' + (dayShows.length - 3) + ' more')); }
				if (!dayShows.length && openDays[key]) { cell.appendChild(el('div', 'tcal-open-tag', 'OPEN')); }
				grid.appendChild(cell);
			}
			wrap.appendChild(grid);

			var count = visible.length;
			wrap.appendChild(el('div', 'tcal-count', count + (count === 1 ? ' show' : ' shows') + ' across ' + selectedArtists().length + ' selected'));
			return wrap;
		}

		function navBtn(label, delta) {
			var b = el('button', 'tcal-cal-arrow', label);
			b.type = 'button';
			b.addEventListener('click', function () {
				state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth() + delta, 1);
				renderMainOnly();
			});
			return b;
		}

		// Returns a map dateStr->true of OPEN fill-in days for the visible month,
		// only when exactly one artist is selected (mirrors doc.py ±2-day rule).
		function computeOpenDays(visible) {
			var open = {};
			if (selectedArtists().length !== 1) { return open; }
			var year = state.cursor.getFullYear(), month = state.cursor.getMonth();
			var booked = {};
			visible.forEach(function (s) {
				var d = F.parseDate(s.date);
				if (d.getFullYear() === year && d.getMonth() === month) { booked[d.getDate()] = true; }
			});
			Object.keys(booked).forEach(function (dayStr) {
				var day = +dayStr;
				for (var i = 1; i <= 2; i++) {
					[-i, i].forEach(function (delta) {
						var od = new Date(year, month, day + delta);
						if (od.getFullYear() === year && od.getMonth() === month && !booked[od.getDate()]) {
							open[year + '-' + pad2(month + 1) + '-' + pad2(od.getDate())] = true;
						}
					});
				}
			});
			return open;
		}

		/* ---- List ---- */

		function renderList() {
			var wrap = el('div', 'tcal-listwrap');
			var rows = filtered();
			var key = state.sort.key, dir = state.sort.dir;
			rows = rows.slice().sort(function (a, b) {
				var av = (a[key] || '').toLowerCase ? (a[key] || '').toLowerCase() : a[key];
				var bv = (b[key] || '').toLowerCase ? (b[key] || '').toLowerCase() : b[key];
				if (av < bv) { return -1 * dir; }
				if (av > bv) { return 1 * dir; }
				return a.date < b.date ? -1 : 1;
			});

			var table = el('table', 'tcal-table');
			var thead = document.createElement('thead');
			var htr = document.createElement('tr');
			[['date', 'Date'], ['artist', 'Artist'], ['venue', 'Venue'], ['city', 'City'], ['region', 'ST'], ['tickets', 'Tickets']].forEach(function (col) {
				var th = document.createElement('th');
				th.textContent = col[1];
				if (col[0] !== 'tickets') {
					th.className = 'tcal-sortable' + (key === col[0] ? (dir === 1 ? ' sort-asc' : ' sort-desc') : '');
					th.addEventListener('click', function () {
						if (state.sort.key === col[0]) { state.sort.dir *= -1; }
						else { state.sort = { key: col[0], dir: 1 }; }
						renderMainOnly();
					});
				}
				htr.appendChild(th);
			});
			thead.appendChild(htr);
			table.appendChild(thead);

			var tbody = document.createElement('tbody');
			rows.forEach(function (s) {
				var tr = document.createElement('tr');
				tr.appendChild(td(F.fmtMMDDYY(F.parseDate(s.date))));
				tr.appendChild(td(s.artist));
				tr.appendChild(td(s.venue));
				tr.appendChild(td(s.city));
				tr.appendChild(td(s.region));
				var tk = document.createElement('td');
				if (s.ticket_url) {
					var a = document.createElement('a');
					a.href = s.ticket_url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Tickets';
					tk.appendChild(a);
				}
				tr.appendChild(tk);
				tbody.appendChild(tr);
			});
			table.appendChild(tbody);
			wrap.appendChild(table);
			if (!rows.length) { wrap.appendChild(el('div', 'tcal-count', 'No shows match the current filters.')); }
			else { wrap.appendChild(el('div', 'tcal-count', rows.length + (rows.length === 1 ? ' show' : ' shows'))); }
			return wrap;
		}

		/* ---- Copy panel ---- */

		function renderCopy() {
			var wrap = el('div', 'tcal-copy');
			wrap.appendChild(el('div', 'tcal-copy-title', 'Copy for outreach (uses current filters)'));

			var preview = document.createElement('textarea');
			preview.className = 'tcal-preview';
			preview.readOnly = true;
			preview.rows = 8;
			preview.placeholder = 'Pick a format to generate copy-pasteable text…';

			var status = el('span', 'tcal-copy-status', '');

			var formats = [
				['Email dates', function (s) { return F.emailDates(s); }],
				['Zone lists', function (s) { return F.zoneLists(s); }],
				['Open dates', function (s) { return F.openDates(s); }],
				['Simple list', function (s) { return F.simpleList(s); }],
				['List + links', function (s) { return F.simpleList(s, { withLinks: true }); }]
			];

			var btns = el('div', 'tcal-copy-btns');
			formats.forEach(function (fmt) {
				var b = el('button', 'tcal-copy-btn', fmt[0]);
				b.type = 'button';
				b.addEventListener('click', function () {
					var text = fmt[1](filtered());
					preview.value = text;
					copyText(text, status);
				});
				btns.appendChild(b);
			});

			var copyBtn = el('button', 'tcal-copy-again', 'Copy');
			copyBtn.type = 'button';
			copyBtn.addEventListener('click', function () { copyText(preview.value, status); });
			btns.appendChild(copyBtn);
			btns.appendChild(status);

			wrap.appendChild(btns);
			wrap.appendChild(preview);
			return wrap;
		}

		function copyText(text, status) {
			if (!text) { return; }
			function ok() { status.textContent = 'Copied!'; setTimeout(function () { status.textContent = ''; }, 1800); }
			if (navigator.clipboard && navigator.clipboard.writeText) {
				navigator.clipboard.writeText(text).then(ok, function () { fallbackCopy(text, ok); });
			} else { fallbackCopy(text, ok); }
		}

		function fallbackCopy(text, ok) {
			var ta = document.createElement('textarea');
			ta.value = text;
			ta.style.position = 'fixed'; ta.style.opacity = '0';
			document.body.appendChild(ta); ta.select();
			try { document.execCommand('copy'); ok(); } catch (e) { /* noop */ }
			document.body.removeChild(ta);
		}

		render();
	}

	/* -------------------------------------------------------------------- */
	/*  tiny DOM helpers                                                    */
	/* -------------------------------------------------------------------- */

	function el(tag, className, text) {
		var n = document.createElement(tag);
		if (className) { n.className = className; }
		if (text != null) { n.textContent = text; }
		return n;
	}
	function td(text) { var n = document.createElement('td'); n.textContent = text == null ? '' : text; return n; }
	function opt(value, label) { var o = document.createElement('option'); o.value = value; o.textContent = label; return o; }
	function linkBtn(label, fn) {
		var b = el('button', 'tcal-linkbtn', label); b.type = 'button';
		b.addEventListener('click', fn); return b;
	}
	function pad2(n) { return n < 10 ? '0' + n : '' + n; }
	function uniqueSorted(arr) {
		return arr.filter(function (v, i, a) { return v && a.indexOf(v) === i; }).sort();
	}
	function formatUpdated(iso) {
		var d = new Date(iso);
		if (isNaN(d.getTime())) { return iso; }
		return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
	}
})();
