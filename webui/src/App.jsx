import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  deletePlayerAttackDraft,
  getPlayerAttackDraft,
  getSettings,
  getState,
  savePlayerAttackDraft,
  saveSettings,
  setControl,
} from "./api.js";

function fmt(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h} ч ${m % 60} мин`;
  }
  return m ? `${m} мин ${s} с` : `${s} с`;
}

function shotName(path) {
  if (!path) return "";
  return String(path).split(/[\\/]/).pop();
}

const STOP_CONDITIONS = [
  { id: "no_commanders", label: "Нет военачальников" },
  { id: "insufficient_troops", label: "Нехватка войск" },
  { id: "target_unavailable", label: "Цель недоступна" },
];

function campaignProgress(state) {
  const row = state?.attacks_world;
  if (row && (row.quota || row.sent)) {
    return `${row.sent || 0}/${row.quota || 0} · осталось ${row.remaining ?? 0}`;
  }
  return String(state?.session_attacks ?? 0);
}

function timingLine(state) {
  if (state?.timing_summary) return state.timing_summary;
  const cycle = state?.action_timings?.attack_cycle;
  if (!cycle) return "—";
  return `последний ${cycle.last_seconds}с · средний ${cycle.average_seconds}с · n=${cycle.count}`;
}

function Toggle({ checked, onChange, disabled, label }) {
  return (
    <label className={`switch ${disabled ? "disabled" : ""}`}>
      <input
        type="checkbox"
        checked={!!checked}
        disabled={disabled}
        aria-label={label}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="slider" />
    </label>
  );
}

export default function App() {
  const [settings, setSettings] = useState(null);
  const [state, setState] = useState(null);
  const [campaign, setCampaign] = useState([]);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [dryRun, setDryRun] = useState(false);
  const [feathers, setFeathers] = useState(true);
  const [maxConcurrent, setMaxConcurrent] = useState(30);
  const [delayMin, setDelayMin] = useState(8);
  const [delayMax, setDelayMax] = useState(10);
  const [playerDraft, setPlayerDraft] = useState({
    world: "great_empire",
    x: 500,
    y: 500,
    attacks: 1,
    commander: "auto",
    formation: "default",
    waves: 1,
    flank: "center",
    tools: "none",
    delay_seconds: 10,
    schedule: "",
    stop_conditions: ["no_commanders", "insufficient_troops"],
  });
  const [draftStatus, setDraftStatus] = useState("");
  const [draftConfirmed, setDraftConfirmed] = useState(false);

  const worlds = settings?.worlds || [];
  const flags = useMemo(() => {
    const map = {};
    campaign.forEach((item) => {
      map[item.mode] = !!item.enabled;
    });
    return map;
  }, [campaign]);

  const persist = useCallback(async (nextCampaign, extra = {}) => {
    setSaving(true);
    savingRef.current = true;
    try {
      const saved = await saveSettings({ campaign: nextCampaign, ...extra });
      setSettings(saved);
      setCampaign(saved.campaign || nextCampaign);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, []);

  useEffect(() => {
    getSettings()
      .then((data) => {
        setSettings(data);
        setCampaign(data.campaign || []);
        setDryRun(!!data.dry_run);
        setFeathers(data.use_feathers !== false);
        setMaxConcurrent(data.max_concurrent_attacks || 30);
        setDelayMin(data.attack_delay_min || 8);
        setDelayMax(data.attack_delay_max || 10);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    getPlayerAttackDraft().then((data) => {
      if (data.draft) setPlayerDraft(data.draft);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const data = await getState();
        if (cancelled) return;
        setState(data);
        setDryRun(!!data.dry_run);
        if (data.campaign?.steps && !savingRef.current) {
          setCampaign(
            data.campaign.steps.map((step) => ({
              mode: step.mode,
              enabled: !!step.enabled,
              count: step.count,
              title_ru: step.title_ru,
            }))
          );
        }
      } catch {
        /* panel still useful if a poll misses */
      }
    }
    tick();
    const id = setInterval(tick, 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  function setMode(id, on) {
    let found = false;
    const next = campaign.map((item) => {
      if (item.mode === id) {
        found = true;
        return { ...item, enabled: on };
      }
      return item;
    });
    if (!found) next.push({ mode: id, enabled: on, count: 10 });
    setCampaign(next);
    persist(next);
  }

  function setWorld(world, on) {
    const ids = new Set((world.modes || []).map((mode) => mode.id));
    const next = campaign.map((item) => (ids.has(item.mode) ? { ...item, enabled: on } : item));
    (world.modes || []).forEach((mode) => {
      if (!next.some((item) => item.mode === mode.id)) {
        next.push({ mode: mode.id, enabled: on, count: mode.default_quota || 10 });
      }
    });
    setCampaign(next);
    persist(next);
  }

  async function onSave() {
    await persist(campaign, {
      max_concurrent_attacks: Number(maxConcurrent),
      attack_delay_min: Number(delayMin),
      attack_delay_max: Number(delayMax),
      dry_run: dryRun,
      use_feathers: feathers,
    });
  }

  const running = state?.enabled !== false && !state?.paused;
  const enabledSteps = (state?.campaign?.steps || campaign).filter((step) => step.enabled);
  const shot = shotName(state?.last_screenshot);

  return (
    <div className="page">
      <header className="hero">
        <div>
          <h1>EmpireBot</h1>
          <p className="sub">
            FastAPI + React. Включи миры и ивенты тумблерами, затем <b>Num1</b>. Пауза — <b>Num0</b>. Раскладка не важна.
          </p>
        </div>
        <div className={`pulse ${running ? "go" : "stop"}`}>{state?.status_label || "загрузка"}</div>
      </header>

      <section className="grid status">
        <article>
          <span>Состояние</span>
          <strong>{state?.status_label || "—"}</strong>
        </article>
        <article>
          <span>Режим</span>
          <strong>{state?.active_mode || state?.mode || "idle"}</strong>
        </article>
        <article>
          <span>Последнее действие</span>
          <strong>{state?.last_action || "—"}</strong>
        </article>
        <article>
          <span>В пути</span>
          <strong>
            {state?.in_flight ?? 0}
            {state?.dry_run ? " · DRY-RUN" : ""}
          </strong>
        </article>
        <article><span>Текущий мир</span><strong>{state?.current_world || "—"}</strong></article>
        <article><span>Фаза</span><strong>{state?.phase || "—"}</strong></article>
        <article><span>Следующее действие</span><strong>{state?.next_action || "—"}</strong></article>
        <article><span>Пауза / включено</span><strong>{state?.paused ? "пауза" : "включено"} · {state?.enabled === false ? "выкл" : "вкл"}</strong></article>
        <article><span>Атаки / мир</span><strong>{campaignProgress(state)}</strong></article>
        <article><span>Атак за сессию</span><strong>{state?.session_attacks ?? 0}</strong></article>
        <article><span>Тайминги цикла</span><strong>{timingLine(state)}</strong></article>
        <article><span>Ошибка / восстановление</span><strong>{state?.last_error || "нет"}</strong></article>
      </section>

      <div className="coords">{state?.last_coords || "—"}</div>
      {state?.current_world ? <div className="hint">Мир: {state.current_world}</div> : null}
      {state?.attack_report ? (
        <section className="card">
          <h2>Отчёт по мирам</h2>
          <pre className="report">{state.attack_report}</pre>
        </section>
      ) : null}
      <section className="card">
        <h2>Сводный отчёт фермы</h2>
        <div className="grid status">
          <article><span>Обработано отчётов</span><strong>{state?.farm_summary?.reports ?? state?.reports_processed ?? 0}</strong></article>
          <article><span>Свои потери</span><strong>{state?.farm_summary?.own_losses ?? 0}</strong></article>
        </div>
        <div className="loot-grid">
          {Object.entries(state?.farm_summary?.resources || {}).map(([name, value]) => (
            <div key={name}><span>{name}</span><strong>{value}</strong></div>
          ))}
        </div>
        {Object.entries(state?.farm_summary?.by_world || {}).map(([world, row]) => (
          <div className="march" key={world}>
            <b>{world}</b> · отчётов {row.reports} · потери {row.own_losses}
          </div>
        ))}
      </section>

      <section className="binds">
        <div className="bind">
          <kbd>{state?.start_hotkey_label || settings?.start_hotkey_label || "Num1"}</kbd>
          <small>старт выбранных задач</small>
        </div>
        <div className="bind">
          <kbd>{state?.pause_hotkey_label || settings?.pause_hotkey_label || "Num0"}</kbd>
          <small>пауза, мышь свободна</small>
        </div>
      </section>

      <div className="chips">
        {enabledSteps.length ? (
          enabledSteps.map((step) => (
            <span className="chip on" key={step.mode}>
              {step.campaign_priority ? `P${step.campaign_priority} · ` : ""}
              {step.title_ru || step.mode}
            </span>
          ))
        ) : (
          <span className="chip">ничего не выбрано</span>
        )}
      </div>

      {shot ? <img className="shot" src={`/shots/${shot}?t=${Date.now()}`} alt="Скрин" /> : null}

      <section className="card">
        <button className={running ? "on" : "off"} type="button" onClick={() => setControl({ enabled: !running })}>
          {running ? "РАБОТАЕТ — ищет цели" : "ПАУЗА — мышь свободна"}
        </button>
        <p className="hint">
          {(state?.start_hotkey_label || "Num1") + " — старт · " + (state?.pause_hotkey_label || "Num0") + " — пауза (цифровой блок, NumLock не важен)"}
        </p>
        <label className="check">
          <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} /> DRY-RUN
        </label>
        <label className="check">
          <input type="checkbox" checked={feathers} onChange={(e) => setFeathers(e.target.checked)} /> Перья
        </label>
        <label className="field">
          Макс. атак в пути
          <input type="number" min="1" max="30" value={maxConcurrent} onChange={(e) => setMaxConcurrent(e.target.value)} />
        </label>
        <label className="field">
          Пауза между атаками, сек
          <div className="row">
            <input type="number" min="1" max="60" value={delayMin} onChange={(e) => setDelayMin(e.target.value)} />
            <input type="number" min="1" max="60" value={delayMax} onChange={(e) => setDelayMax(e.target.value)} />
          </div>
        </label>
        <button className="save" type="button" onClick={onSave} disabled={saving}>
          Сохранить настройки
        </button>
      </section>

      <section className="card">
        <h2>Миры и ивенты</h2>
        <p className="hint">
          Тумблер включает задачу. Среди включённых: P1 кочевники → самураи (если включены), затем Великая империя → ледник → пески → вершины → острова ураганов, затем P4 чужеземцы. Выключенное не запускается.
        </p>
        {worlds.map((world) => {
          const modes = world.modes || [];
          const worldOn = modes.length > 0 && modes.every((mode) => flags[mode.id]);
          const worldPartial = modes.some((mode) => flags[mode.id]) && !worldOn;
          return (
            <div className="world" key={world.kingdom_ru}>
              <div className="world-head">
                <div>
                  <div className="world-title">{world.kingdom_ru}</div>
                  <div className="world-en">{world.kingdom_en}</div>
                </div>
                <Toggle checked={worldOn} onChange={(on) => setWorld(world, on)} label={`Мир ${world.kingdom_ru}`} />
              </div>
              {worldPartial ? <div className="partial">часть задач включена</div> : null}
              {modes.map((mode) => (
                <div className="toggle-row" key={mode.id}>
                  <div className="meta">
                    <div className="name">
                      {mode.campaign_priority ? <span className="badge prio">P{mode.campaign_priority}</span> : null}
                      {mode.title_ru}
                      <span className={`badge ${mode.status === "live" ? "live" : "stub"}`}>
                        {mode.status === "live" ? "live" : "заглушка"}
                      </span>
                    </div>
                    <div className="hint">{mode.official_name}</div>
                  </div>
                  <Toggle checked={!!flags[mode.id]} onChange={(on) => setMode(mode.id, on)} label={mode.title_ru} />
                </div>
              ))}
            </div>
          );
        })}
      </section>

      <section className="card player-draft">
        <h2>Атака игрока <span className="badge stub">В разработке</span></h2>
        <p className="warn">
          Атаки на игроков не реализованы. Кнопка запуска отключена. Можно только сохранить черновик через FastAPI.
        </p>
        <div className="form-grid">
          <label className="field">
            Мир
            <select value={playerDraft.world} onChange={(e) => setPlayerDraft({ ...playerDraft, world: e.target.value })}>
              <option value="great_empire">Великая империя</option>
              <option value="everwinter">Вечнохолодный ледник</option>
              <option value="burning_sands">Пылающие пески</option>
              <option value="fire_peaks">Огненные вершины</option>
              <option value="storm_islands">Острова ураганов</option>
            </select>
          </label>
          <label className="field">
            X
            <input type="number" min="0" max="999" value={playerDraft.x} onChange={(e) => setPlayerDraft({ ...playerDraft, x: e.target.value })} />
          </label>
          <label className="field">
            Y
            <input type="number" min="0" max="999" value={playerDraft.y} onChange={(e) => setPlayerDraft({ ...playerDraft, y: e.target.value })} />
          </label>
          <label className="field">
            Количество атак
            <input type="number" min="1" max="20" value={playerDraft.attacks} onChange={(e) => setPlayerDraft({ ...playerDraft, attacks: e.target.value })} />
          </label>
          <label className="field">
            Военачальник
            <select
              value={playerDraft.commander === "auto" || playerDraft.commander === "" || playerDraft.commander == null ? "auto" : "number"}
              onChange={(e) => setPlayerDraft({ ...playerDraft, commander: e.target.value === "auto" ? "auto" : 1 })}
            >
              <option value="auto">Auto</option>
              <option value="number">Номер</option>
            </select>
          </label>
          {playerDraft.commander !== "auto" && playerDraft.commander !== "" && playerDraft.commander != null ? (
            <label className="field">
              Номер военачальника
              <input
                type="number"
                min="1"
                max="99"
                value={playerDraft.commander}
                onChange={(e) => setPlayerDraft({ ...playerDraft, commander: e.target.value })}
              />
            </label>
          ) : null}
          <label className="field">
            Формация / пресет
            <select value={playerDraft.formation} onChange={(e) => setPlayerDraft({ ...playerDraft, formation: e.target.value })}>
              <option value="default">По умолчанию</option>
              <option value="saved_1">Сохранённая 1</option>
            </select>
          </label>
          <label className="field">
            Волны
            <input type="number" min="1" max="6" value={playerDraft.waves} onChange={(e) => setPlayerDraft({ ...playerDraft, waves: e.target.value })} />
          </label>
          <label className="field">
            Фланги
            <select value={playerDraft.flank} onChange={(e) => setPlayerDraft({ ...playerDraft, flank: e.target.value })}>
              <option value="left">Левый</option>
              <option value="center">Центр</option>
              <option value="right">Правый</option>
              <option value="all">Все</option>
            </select>
          </label>
          <label className="field">
            Инструменты
            <select value={playerDraft.tools} onChange={(e) => setPlayerDraft({ ...playerDraft, tools: e.target.value })}>
              <option value="none">Без инструментов</option>
              <option value="preset_1">Пресет 1</option>
            </select>
          </label>
          <label className="field">
            Задержка, сек
            <input type="number" min="0" max="86400" value={playerDraft.delay_seconds} onChange={(e) => setPlayerDraft({ ...playerDraft, delay_seconds: e.target.value })} />
          </label>
          <label className="field">
            Расписание
            <input value={playerDraft.schedule} placeholder="не задано" onChange={(e) => setPlayerDraft({ ...playerDraft, schedule: e.target.value })} />
          </label>
        </div>
        <div className="check-row">
          {STOP_CONDITIONS.map((item) => (
            <label className="check" key={item.id}>
              <input
                type="checkbox"
                checked={(playerDraft.stop_conditions || []).includes(item.id)}
                onChange={(e) =>
                  setPlayerDraft({
                    ...playerDraft,
                    stop_conditions: e.target.checked
                      ? [...new Set([...(playerDraft.stop_conditions || []), item.id])]
                      : (playerDraft.stop_conditions || []).filter((id) => id !== item.id),
                  })
                }
              />
              {item.label}
            </label>
          ))}
        </div>
        <div className="summary">
          Подтверждение черновика: {playerDraft.world} · ({playerDraft.x}, {playerDraft.y}) · атак {playerDraft.attacks} · командир{" "}
          {playerDraft.commander || "auto"} · волн {playerDraft.waves} · {playerDraft.flank}
        </div>
        <label className="check">
          <input type="checkbox" checked={draftConfirmed} onChange={(e) => setDraftConfirmed(e.target.checked)} />
          Подтверждаю: это только черновик, атаки на игроков не запускаются
        </label>
        <div className="row">
          <button
            className="save"
            type="button"
            disabled={!draftConfirmed}
            onClick={async () => {
              try {
                const result = await savePlayerAttackDraft(playerDraft);
                setPlayerDraft(result.draft);
                setDraftStatus("Черновик сохранён. Запуск атак на игроков не выполнялся.");
              } catch {
                setDraftStatus("Проверь поля черновика.");
              }
            }}
          >
            Сохранить черновик
          </button>
          <button
            className="delete"
            type="button"
            onClick={async () => {
              await deletePlayerAttackDraft();
              setDraftStatus("Черновик удалён.");
            }}
          >
            Удалить
          </button>
        </div>
        <button type="button" disabled>
          Запуск атак на игроков пока не реализован
        </button>
        {draftStatus ? <p className="ok">{draftStatus}</p> : null}
      </section>

      <section className="card">
        <h2>Военачальники в походе</h2>
        {(state?.marches || []).length ? (
          state.marches.map((m) => (
            <div className="march" key={`${m.commander_no}-${m.x}-${m.y}`}>
              <b>№{m.commander_no}</b> · {m.kind} · K{m.kingdom} ({m.x}, {m.y})
              <br />
              <span className="ok">до цели {fmt(m.arrive_left_sec)}</span> ·{" "}
              <span className="warn">возврат {fmt(m.return_left_sec)}</span>
            </div>
          ))
        ) : (
          <p className="hint">Свободных маршей нет</p>
        )}
      </section>
    </div>
  );
}
