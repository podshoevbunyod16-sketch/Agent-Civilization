import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  type Account,
  type AccountStatement,
  type LedgerVerification,
  type RewardGrant,
  type RewardRule,
  type TransactionWithImpact,
} from '@/api/client'
import { Badge, Empty, ErrorBox, Loader, PageHead, Stat } from '@/components/ui'
import { useAsync } from '@/lib/useAsync'
import { useAuth } from '@/lib/auth'
import { formatDate, label, relativeTime, truncate } from '@/lib/format'

function units(value: number): string {
  return `${value.toFixed(2)} ACU`
}

export default function Economy() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  const money = useAsync((signal) => api.myMoney(signal), [])
  const accounts = useAsync((signal) => api.accounts(signal), [])
  const stats = useAsync((signal) => api.economyStats(signal), [])
  const rules = useAsync((signal) => api.rewardRules(signal), [])
  const rewards = useAsync((signal) => api.rewards({ limit: 20 }, signal), [])

  const [selectedAccount, setSelectedAccount] = useState<string | null>(null)
  const activeAccount = selectedAccount ?? money.data?.account_id ?? null
  const statement = useAsync<AccountStatement | null>(
    (signal) => (activeAccount ? api.accountStatement(activeAccount, signal) : Promise.resolve(null)),
    [activeAccount],
  )
  const history = useAsync(
    (signal) => api.transactions({ account_id: activeAccount ?? undefined, limit: 25 }, signal),
    [activeAccount],
  )

  const [verification, setVerification] = useState<LedgerVerification | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Перевод
  const [toAccount, setToAccount] = useState('')
  const [amount, setAmount] = useState('10')
  const [comment, setComment] = useState('')

  // Вывод заработка
  const [withdrawAgent, setWithdrawAgent] = useState('')
  const [withdrawAmount, setWithdrawAmount] = useState('1')

  const reload = () => {
    money.reload()
    accounts.reload()
    stats.reload()
    rewards.reload()
    history.reload()
    statement.reload()
  }

  const act = async (key: string, fn: () => Promise<unknown>, message: string) => {
    setBusy(key)
    setError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(message)
      reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Операция не выполнена')
    } finally {
      setBusy(null)
    }
  }

  const doTransfer = () => {
    if (!activeAccount || !toAccount) {
      setError('Выберите счёт-источник и счёт-получатель')
      return
    }
    const value = Number(amount)
    if (!Number.isFinite(value) || value <= 0) {
      setError('Сумма перевода должна быть положительной')
      return
    }
    void act(
      'transfer',
      () =>
        api.transfer({
          from_account_id: activeAccount,
          to_account_id: toAccount,
          amount_units: value,
          comment,
        }),
      `Перевод ${units(value)} выполнен`,
    )
    setComment('')
  }

  const doWithdraw = () => {
    const agent = withdrawAgent || money.data?.agents[0]?.agent_id
    if (!agent) {
      setError('Нет агента с заработком')
      return
    }
    const value = Number(withdrawAmount)
    if (!Number.isFinite(value) || value <= 0) {
      setError('Сумма вывода должна быть положительной')
      return
    }
    void act('withdraw', () => api.withdraw(agent, value), `Выведено ${units(value)}`)
  }

  const doVerify = () => {
    void act(
      'verify',
      async () => {
        const report = await api.verifyLedger()
        setVerification(report)
        return report
      },
      'Сверка выполнена',
    )
  }

  const doReverse = (transactionId: string) => {
    void act(
      'reverse',
      () => api.reverseTransaction(transactionId, 'решение администратора'),
      'Операция отменена обратной транзакцией',
    )
  }

  const ownAccounts: Account[] = accounts.data ?? []
  const transferTargets = ownAccounts.filter((account) => account.id !== activeAccount)

  return (
    <div className="stack">
      <PageHead
        title="Экономика"
        subtitle="Внутренние расчётные единицы (ACU) — учёт вклада и стоимости, не деньги и не криптовалюта. Каждая операция — двойная запись."
        actions={
          <>
            <button className="btn" disabled={busy !== null} onClick={doVerify}>
              Сверить журнал
            </button>
            <button className="btn" onClick={reload}>
              Обновить
            </button>
          </>
        }
      />

      {error ? <ErrorBox message={error} /> : null}
      {notice ? <div className="alert ok">{notice}</div> : null}

      {verification ? (
        <div className={`alert ${verification.ok ? 'ok' : ''}`}>
          <strong>{verification.ok ? 'Журнал сходится' : 'Обнаружено расхождение'}</strong>
          <div className="faint" style={{ marginTop: 6 }}>
            несходящихся транзакций: {verification.unbalanced_transactions.length} ·
            расхождений счетов: {verification.accounts_mismatch.length} · сумма всех балансов:{' '}
            {verification.total_units.toFixed(4)} (обязана быть 0) · проверено{' '}
            {formatDate(verification.checked_at)}
          </div>
        </div>
      ) : null}

      {money.loading && !money.data ? <Loader rows={2} /> : null}
      {money.error ? <ErrorBox message={money.error} onRetry={money.reload} /> : null}

      {money.data ? (
        <div className="grid stats">
          <Stat
            label="Мой бюджет"
            value={units(money.data.balance_units)}
            hint="счёт участника"
          />
          <Stat
            label="Заработок агентов"
            value={units(money.data.agents.reduce((sum, item) => sum + item.balance_units, 0))}
            hint={`агентов со счётом: ${money.data.agents.length}`}
          />
          <Stat
            label="Получено наград"
            value={units(money.data.rewards.units)}
            hint={`начислений: ${money.data.rewards.count}`}
          />
          <Stat
            label="В казначействе"
            value={units(stats.data?.accounts.treasury?.balance ?? 0)}
            hint="эмиссионный счёт (может быть отрицательным)"
          />
        </div>
      ) : null}

      <div className="grid two">
        <div className="card stack">
          <div className="card-title">Мои счета</div>
          {ownAccounts.length === 0 ? (
            <Empty>Счетов нет.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Счёт</th>
                    <th>Тип</th>
                    <th>Баланс</th>
                    <th>Статус</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {ownAccounts.map((account) => (
                    <tr key={account.id}>
                      <td>
                        <div>{account.label}</div>
                        <div className="faint" style={{ fontSize: 11 }}>
                          {account.id.slice(0, 8)}
                        </div>
                      </td>
                      <td>{label(account.type)}</td>
                      <td>{units(account.balance_units)}</td>
                      <td>
                        <Badge value={account.status} />
                      </td>
                      <td>
                        <button
                          className="btn btn-sm"
                          onClick={() => setSelectedAccount(account.id)}
                          disabled={account.id === activeAccount}
                        >
                          Выписка
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="row between" style={{ marginTop: 8 }}>
            <div className="card-title">Перевод</div>
            <span className="faint">идемпотентность обеспечивает база</span>
          </div>
          <div className="form-grid">
            <div className="field">
              <label>Со счёта</label>
              <select
                className="select"
                value={activeAccount ?? ''}
                onChange={(event) => setSelectedAccount(event.target.value)}
              >
                {ownAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.label} · {account.balance_units.toFixed(2)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>На счёт</label>
              <select
                className="select"
                value={toAccount}
                onChange={(event) => setToAccount(event.target.value)}
              >
                <option value="">— выберите —</option>
                {transferTargets.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Сумма, ACU</label>
              <input
                className="input"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                inputMode="decimal"
              />
            </div>
            <div className="field">
              <label>Комментарий</label>
              <input
                className="input"
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="за что перевод"
              />
            </div>
          </div>
          <div className="row">
            <button className="btn btn-primary" disabled={busy !== null} onClick={doTransfer}>
              Перевести
            </button>
            <button className="btn" disabled={busy !== null} onClick={doVerify}>
              Сверить журнал
            </button>
          </div>
        </div>

        <div className="card stack">
          <div className="card-title">Вывод заработка</div>
          <div className="faint">
            Агент зарабатывает на оплаченных заданиях. Владелец переводит заработок на свой счёт;
            вывод не создаёт новых единиц — только перемещает их между счетами.
          </div>
          <div className="form-grid">
            <div className="field">
              <label>Агент</label>
              <select
                className="select"
                value={withdrawAgent}
                onChange={(event) => setWithdrawAgent(event.target.value)}
              >
                {(money.data?.agents ?? []).map((item) => (
                  <option key={item.agent_id} value={item.agent_id}>
                    {item.name} · {item.balance_units.toFixed(2)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Сумма, ACU</label>
              <input
                className="input"
                value={withdrawAmount}
                onChange={(event) => setWithdrawAmount(event.target.value)}
                inputMode="decimal"
              />
            </div>
          </div>
          <button
            className="btn"
            disabled={busy !== null || (money.data?.agents.length ?? 0) === 0}
            onClick={doWithdraw}
          >
            Вывести на мой счёт
          </button>

          <div className="card-title" style={{ marginTop: 8 }}>
            Правила наград
          </div>
          {rules.data && rules.data.length === 0 ? (
            <Empty>Правила не настроены — начисления не производятся.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Правило</th>
                    <th>Награда</th>
                    <th>Бонус за уровень</th>
                    <th>Комиссия</th>
                    <th>Лимит в день</th>
                  </tr>
                </thead>
                <tbody>
                  {(rules.data ?? []).map((rule: RewardRule) => (
                    <tr key={rule.id}>
                      <td>{rule.name}</td>
                      <td>{units(rule.verified_reward_units)}</td>
                      <td>{units(rule.capability_bonus_units)}</td>
                      <td>{(rule.platform_fee_ratio * 100).toFixed(1)}%</td>
                      <td>{units(rule.daily_cap_units)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="faint" style={{ fontSize: 12 }}>
            Награда начисляется только за подтверждённый результат (вердикт «verified»), один раз
            на задание. Админ может менять ставки через API; изменение попадает в аудит.
          </div>
        </div>
      </div>

      <div className="card stack">
        <div className="row between">
          <div className="card-title">История операций</div>
          <span className="faint">
            {statement.data
              ? `поступления ${units(statement.data.total_in)} · списания ${units(statement.data.total_out)}`
              : ''}
          </span>
        </div>
        {history.loading && !history.data ? <Loader rows={2} /> : null}
        {history.error ? <ErrorBox message={history.error} onRetry={history.reload} /> : null}
        {history.data && history.data.length === 0 ? <Empty>Операций пока нет.</Empty> : null}
        {history.data && history.data.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Когда</th>
                  <th>Тип</th>
                  <th>Сумма</th>
                  <th>Влияние</th>
                  <th>Описание</th>
                  <th>Связь</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {history.data.map((item: TransactionWithImpact) => (
                  <tr key={item.transaction.id}>
                    <td className="faint">{relativeTime(item.transaction.created_at)}</td>
                    <td>
                      {label(item.transaction.kind)}
                      {item.transaction.status === 'reversed' ? (
                        <div>
                          <Badge value="reversed" />
                        </div>
                      ) : null}
                    </td>
                    <td>{units(item.transaction.amount_units)}</td>
                    <td>
                      <span className={item.impact >= 0 ? 'delta-pos' : 'delta-neg'}>
                        {item.impact > 0 ? '+' : ''}
                        {item.impact.toFixed(2)}
                      </span>
                    </td>
                    <td>{truncate(item.transaction.description, 70)}</td>
                    <td>
                      {item.transaction.task_id ? (
                        <Link className="link" to={`/tasks/${item.transaction.task_id}`}>
                          задание
                        </Link>
                      ) : null}
                    </td>
                    <td>
                      {isAdmin &&
                      item.transaction.status === 'posted' &&
                      item.transaction.kind !== 'reversal' ? (
                        <button
                          className="btn btn-sm"
                          disabled={busy !== null}
                          onClick={() => doReverse(item.transaction.id)}
                        >
                          Отменить
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      <div className="grid two">
        <div className="card stack">
          <div className="card-title">Сводка по операциям</div>
          {stats.data ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Тип</th>
                    <th>Операций</th>
                    <th>Объём</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(stats.data.transactions).map(([kind, value]) => (
                    <tr key={kind}>
                      <td>{label(kind)}</td>
                      <td>{value.count}</td>
                      <td>{units(value.volume)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Loader rows={1} />
          )}
        </div>

        <div className="card stack">
          <div className="card-title">Начисления наград</div>
          {rewards.data && rewards.data.length === 0 ? (
            <Empty>Наград пока нет: они начисляются за подтверждённые результаты.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Когда</th>
                    <th>Задание</th>
                    <th>Сумма</th>
                    <th>Состав</th>
                  </tr>
                </thead>
                <tbody>
                  {(rewards.data ?? []).map((grant: RewardGrant) => (
                    <tr key={grant.id}>
                      <td className="faint">{relativeTime(grant.created_at)}</td>
                      <td>
                        <Link className="link" to={`/tasks/${grant.task_id}`}>
                          {grant.task_id.slice(0, 8)}
                        </Link>
                      </td>
                      <td>{units(grant.amount_units)}</td>
                      <td className="faint" style={{ fontSize: 12 }}>
                        база {grant.base_units.toFixed(2)} + бонус {grant.bonus_units.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {stats.data ? (
            <div className="faint" style={{ fontSize: 12 }}>
              заморожено бюджетов: {stats.data.held_budgets.count} на сумму{' '}
              {units(stats.data.held_budgets.units)}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
