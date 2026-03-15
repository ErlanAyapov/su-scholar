export default function ResearchManagementUIMockup() {
  const nav = [
    "Профиль исследователя",
    "Dashboard кафедры",
    "Dashboard института",
    "Dashboard университета",
  ];

  const kpis = [
    { label: "Публикации", value: "128", sub: "+14%" },
    { label: "Цитирования", value: "2 436", sub: "+9%" },
    { label: "Гранты", value: "18", sub: "+3" },
    { label: "Финансирование", value: "₸ 486 млн", sub: "+11%" },
  ];

  const collaborators = [
    { name: "А. А. Иванов", dept: "Кибербезопасность", pubs: 6, projects: 1 },
    { name: "М. К. Садыкова", dept: "Автоматизация", pubs: 4, projects: 2 },
    { name: "Е. Т. Нургалиев", dept: "Робототехника", pubs: 3, projects: 1 },
  ];

  const deptRows = [
    ["Кибербезопасность", "46", "812", "7", "₸ 118 млн"],
    ["Автоматизация", "38", "654", "5", "₸ 92 млн"],
    ["Информационные системы", "31", "521", "4", "₸ 76 млн"],
    ["Электроника", "24", "280", "2", "₸ 44 млн"],
  ];

  const instituteRows = [
    ["ИАиИТ", "139", "2 843", "16", "₸ 330 млн"],
    ["Горно-металлургический", "97", "1 506", "11", "₸ 248 млн"],
    ["Энергетика и машиностроение", "82", "1 133", "8", "₸ 194 млн"],
  ];

  const years = [2020, 2021, 2022, 2023, 2024];
  const publications = [42, 51, 58, 71, 84];
  const funding = [72, 95, 110, 132, 158];

  const MiniBars = ({ values, max }) => (
    <div className="flex items-end gap-2 h-28">
      {values.map((v, i) => (
        <div key={i} className="flex-1 flex flex-col items-center gap-2">
          <div
            className="w-full rounded-2xl bg-slate-900/90"
            style={{ height: `${(v / max) * 100}%` }}
          />
          <span className="text-xs text-slate-500">{years[i]}</span>
        </div>
      ))}
    </div>
  );

  const KpiCard = ({ label, value, sub }) => (
    <div className="rounded-3xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <div className="text-sm text-slate-500">{label}</div>
      <div className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">{value}</div>
      <div className="mt-2 text-sm text-slate-600">{sub} к прошлому периоду</div>
    </div>
  );

  const SectionTitle = ({ title, subtitle }) => (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div>
        <h2 className="text-xl font-semibold text-slate-900">{title}</h2>
        <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
      </div>
      <button className="rounded-2xl border border-slate-200 px-4 py-2 text-sm font-medium text-slate-700">
        Экспорт отчета
      </button>
    </div>
  );

  return (
    <div className="min-h-screen bg-slate-100 p-6 text-slate-900">
      <div className="mx-auto max-w-7xl rounded-[28px] bg-white shadow-xl ring-1 ring-slate-200 overflow-hidden">
        <div className="grid min-h-screen grid-cols-12">
          <aside className="col-span-12 border-b border-slate-200 bg-slate-950 p-6 text-white md:col-span-3 md:border-b-0 md:border-r">
            <div className="mb-8">
              <div className="text-xs uppercase tracking-[0.2em] text-slate-400">Research Management</div>
              <div className="mt-2 text-2xl font-semibold">UI Mockup</div>
              <div className="mt-2 text-sm text-slate-400">Профили, KPI, коллаборации и уровни аналитики</div>
            </div>

            <nav className="space-y-2">
              {nav.map((item, idx) => (
                <div
                  key={item}
                  className={`rounded-2xl px-4 py-3 text-sm ${idx === 0 ? "bg-white text-slate-950" : "text-slate-300 hover:bg-slate-900"}`}
                >
                  {item}
                </div>
              ))}
            </nav>

            <div className="mt-8 rounded-3xl bg-white/5 p-4 text-sm text-slate-300">
              <div className="font-medium text-white">Фильтры</div>
              <div className="mt-3 space-y-2">
                <div className="rounded-2xl border border-white/10 px-3 py-2">Период: 2020–2024</div>
                <div className="rounded-2xl border border-white/10 px-3 py-2">Тип: публикации + гранты</div>
                <div className="rounded-2xl border border-white/10 px-3 py-2">Статус: внутренние и внешние связи</div>
              </div>
            </div>
          </aside>

          <main className="col-span-12 bg-slate-50 p-6 md:col-span-9 lg:p-8">
            <div className="space-y-10">
              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="1. Профиль исследователя"
                  subtitle="Карточка исследователя с научными результатами, KPI и коллаборациями"
                />

                <div className="grid gap-6 lg:grid-cols-12">
                  <div className="lg:col-span-8 space-y-6">
                    <div className="rounded-3xl border border-slate-200 bg-slate-50 p-5">
                      <div className="flex flex-col gap-5 md:flex-row md:items-start">
                        <div className="h-28 w-28 shrink-0 rounded-3xl bg-slate-200" />
                        <div className="flex-1">
                          <div className="flex flex-wrap items-center gap-3">
                            <h3 className="text-2xl font-semibold">Нуртай Албанбай</h3>
                            <span className="rounded-full bg-slate-900 px-3 py-1 text-xs font-medium text-white">Профессор</span>
                          </div>
                          <p className="mt-2 text-sm text-slate-600">
                            Институт автоматики и информационных технологий · Кафедра кибербезопасности
                          </p>
                          <div className="mt-4 flex flex-wrap gap-2 text-sm">
                            {[
                              "ORCID: 0000-0000-0000-0000",
                              "Scopus Author ID",
                              "Web of Science ID",
                              "Google Scholar",
                            ].map((chip) => (
                              <span key={chip} className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-slate-700">
                                {chip}
                              </span>
                            ))}
                          </div>
                          <div className="mt-4 flex flex-wrap gap-2">
                            {["IoT", "LoRaWAN", "Federated Learning", "Cybersecurity", "Energy Harvesting"].map((chip) => (
                              <span key={chip} className="rounded-full bg-slate-100 px-3 py-1.5 text-sm text-slate-700">
                                {chip}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                      {kpis.map((item) => (
                        <KpiCard key={item.label} {...item} />
                      ))}
                    </div>

                    <div className="rounded-3xl border border-slate-200 bg-white p-5">
                      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                        <h3 className="text-lg font-semibold">Научные результаты</h3>
                        <div className="flex flex-wrap gap-2 text-sm">
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Поиск публикаций</div>
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Год</div>
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Тематика</div>
                          <div className="rounded-2xl border border-slate-200 px-3 py-2">Сортировка</div>
                        </div>
                      </div>
                      <div className="space-y-3">
                        {[
                          "Electromagnetic Vibration Energy Harvester with Magnetic Levitation",
                          "Secure Federated Learning on ESP32 using Lightweight Ciphers",
                          "LoRaWAN Smart-City Deployment and Reliability Analysis",
                          "Energy-Efficient IoT Nodes Powered by Vibration Harvesters",
                        ].map((title, idx) => (
                          <div key={title} className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200">
                            <div className="flex items-start justify-between gap-4">
                              <div>
                                <div className="font-medium text-slate-900">{idx + 1}. {title}</div>
                                <div className="mt-1 text-sm text-slate-500">2024 · Article · Q2 · 12 citations</div>
                              </div>
                              <span className="rounded-full bg-white px-3 py-1 text-xs ring-1 ring-slate-200">Подробнее</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="lg:col-span-4 space-y-6">
                    <div className="rounded-3xl border border-slate-200 bg-white p-5">
                      <h3 className="text-lg font-semibold">Коллаборации</h3>
                      <div className="mt-3 rounded-3xl bg-slate-50 p-4 ring-1 ring-slate-200">
                        <div className="text-sm text-slate-500">Внутренние</div>
                        <div className="mt-1 text-3xl font-semibold">18</div>
                        <div className="mt-4 h-40 rounded-[22px] border border-dashed border-slate-300 bg-white flex items-center justify-center text-sm text-slate-400">
                          Граф сети коллабораций
                        </div>
                      </div>
                      <div className="mt-4 space-y-3">
                        {collaborators.map((item) => (
                          <div key={item.name} className="rounded-2xl bg-slate-50 p-4 ring-1 ring-slate-200">
                            <div className="font-medium">{item.name}</div>
                            <div className="mt-1 text-sm text-slate-500">{item.dept}</div>
                            <div className="mt-3 flex gap-2 text-xs text-slate-600">
                              <span className="rounded-full bg-white px-2 py-1 ring-1 ring-slate-200">{item.pubs} публикаций</span>
                              <span className="rounded-full bg-white px-2 py-1 ring-1 ring-slate-200">{item.projects} проект</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-3xl border border-slate-200 bg-white p-5">
                      <h3 className="text-lg font-semibold">Руководству важно</h3>
                      <div className="mt-4 space-y-3 text-sm">
                        {[
                          ["Проекты и гранты", "7"],
                          ["Привлеченное финансирование", "₸ 96 млн"],
                          ["Внешние партнеры", "12"],
                          ["Open Access", "68%"],
                        ].map(([label, value]) => (
                          <div key={label} className="flex items-center justify-between rounded-2xl bg-slate-50 px-4 py-3 ring-1 ring-slate-200">
                            <span className="text-slate-600">{label}</span>
                            <span className="font-semibold text-slate-900">{value}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="2. Dashboard кафедры"
                  subtitle="Понимание, кто дает результат, где рост, а где нужна встряска"
                />
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <KpiCard label="Исследователи" value="32" sub="2 новых профиля" />
                  <KpiCard label="Публикации" value="46" sub="+12%" />
                  <KpiCard label="Гранты" value="7" sub="+2" />
                  <KpiCard label="Финансирование" value="₸ 118 млн" sub="+15%" />
                </div>
                <div className="mt-6 grid gap-6 lg:grid-cols-2">
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Публикации по годам</div>
                    <MiniBars values={publications} max={90} />
                  </div>
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Финансирование по годам</div>
                    <MiniBars values={funding} max={180} />
                  </div>
                </div>
                <div className="mt-6 rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                  <div className="mb-4 text-lg font-semibold">Топ сотрудников кафедры</div>
                  <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-slate-100 text-slate-600">
                        <tr>
                          <th className="px-4 py-3">Исследователь</th>
                          <th className="px-4 py-3">Публикации</th>
                          <th className="px-4 py-3">Цитирования</th>
                          <th className="px-4 py-3">Гранты</th>
                          <th className="px-4 py-3">Коллаборации</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[
                          ["Н. Албанбай", "18", "420", "3", "12"],
                          ["М. Алимова", "14", "310", "2", "8"],
                          ["А. Жунусов", "9", "165", "1", "6"],
                        ].map((row, idx) => (
                          <tr key={idx} className="border-t border-slate-200">
                            {row.map((cell) => (
                              <td key={cell} className="px-4 py-3">{cell}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="3. Dashboard института"
                  subtitle="Сравнение кафедр, сильные направления и межкафедральные связи"
                />
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <KpiCard label="Кафедры" value="8" sub="в структуре института" />
                  <KpiCard label="Публикации" value="139" sub="+10%" />
                  <KpiCard label="Гранты" value="16" sub="+4" />
                  <KpiCard label="Финансирование" value="₸ 330 млн" sub="+13%" />
                </div>
                <div className="mt-6 grid gap-6 lg:grid-cols-[1.3fr_1fr]">
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Сравнение кафедр</div>
                    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                      <table className="w-full text-left text-sm">
                        <thead className="bg-slate-100 text-slate-600">
                          <tr>
                            <th className="px-4 py-3">Кафедра</th>
                            <th className="px-4 py-3">Публикации</th>
                            <th className="px-4 py-3">Цитирования</th>
                            <th className="px-4 py-3">Гранты</th>
                            <th className="px-4 py-3">Финансирование</th>
                          </tr>
                        </thead>
                        <tbody>
                          {deptRows.map((row, idx) => (
                            <tr key={idx} className="border-t border-slate-200">
                              {row.map((cell) => (
                                <td key={cell} className="px-4 py-3">{cell}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                  <div className="space-y-6">
                    <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                      <div className="mb-3 text-lg font-semibold">Межкафедральные коллаборации</div>
                      <div className="h-48 rounded-[22px] border border-dashed border-slate-300 bg-white flex items-center justify-center text-sm text-slate-400">
                        Карта связей кафедр института
                      </div>
                    </div>
                    <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                      <div className="mb-3 text-lg font-semibold">Сильные направления</div>
                      <div className="flex flex-wrap gap-2">
                        {[
                          "Cybersecurity",
                          "AI & Data Science",
                          "Industrial IoT",
                          "Automation",
                          "Smart Systems",
                        ].map((item) => (
                          <span key={item} className="rounded-full bg-white px-3 py-2 text-sm ring-1 ring-slate-200">{item}</span>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              <section className="rounded-[28px] bg-white p-6 shadow-sm ring-1 ring-slate-200">
                <SectionTitle
                  title="4. Dashboard университета"
                  subtitle="Общая картина по науке, KPI, рейтинг подразделений и партнерская сеть"
                />
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <KpiCard label="Институты" value="6" sub="в аналитике" />
                  <KpiCard label="Публикации" value="418" sub="+11%" />
                  <KpiCard label="Международные партнеры" value="57" sub="+8" />
                  <KpiCard label="Финансирование" value="₸ 1.24 млрд" sub="+17%" />
                </div>
                <div className="mt-6 grid gap-6 lg:grid-cols-[1.3fr_1fr]">
                  <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                    <div className="mb-4 text-lg font-semibold">Сравнение институтов</div>
                    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
                      <table className="w-full text-left text-sm">
                        <thead className="bg-slate-100 text-slate-600">
                          <tr>
                            <th className="px-4 py-3">Институт</th>
                            <th className="px-4 py-3">Публикации</th>
                            <th className="px-4 py-3">Цитирования</th>
                            <th className="px-4 py-3">Гранты</th>
                            <th className="px-4 py-3">Финансирование</th>
                          </tr>
                        </thead>
                        <tbody>
                          {instituteRows.map((row, idx) => (
                            <tr key={idx} className="border-t border-slate-200">
                              {row.map((cell) => (
                                <td key={cell} className="px-4 py-3">{cell}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                  <div className="space-y-6">
                    <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                      <div className="mb-3 text-lg font-semibold">Риск-зоны</div>
                      <div className="space-y-3 text-sm">
                        {[
                          "2 кафедры со снижением публикационной динамики",
                          "11 профилей без внешних коллабораций",
                          "Open Access ниже 50% в 1 институте",
                          "3 гранта завершаются в этом квартале",
                        ].map((item) => (
                          <div key={item} className="rounded-2xl bg-white px-4 py-3 ring-1 ring-slate-200">{item}</div>
                        ))}
                      </div>
                    </div>
                    <div className="rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200">
                      <div className="mb-3 text-lg font-semibold">Партнерская сеть</div>
                      <div className="h-48 rounded-[22px] border border-dashed border-slate-300 bg-white flex items-center justify-center text-sm text-slate-400">
                        Международные и внутренние коллаборации
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          </main>
        </div>
      </div>
    </div>
  );
}
