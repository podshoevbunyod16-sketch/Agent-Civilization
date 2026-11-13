import { describe, expect, it } from 'vitest'

import { label, relativeTime, statusTone, truncate } from './format'

describe('label', () => {
  it('переводит известные статусы', () => {
    expect(label('published')).toBe('Опубликован')
    expect(label('succeeded')).toBe('Выполнено')
  })
  it('возвращает прочерк для пустого значения', () => {
    expect(label(null)).toBe('—')
    expect(label(undefined)).toBe('—')
    expect(label('')).toBe('—')
  })
  it('возвращает исходную строку для неизвестного статуса', () => {
    expect(label('something-custom')).toBe('something-custom')
  })
})

describe('truncate', () => {
  it('не трогает короткие строки', () => {
    expect(truncate('abc', 5)).toBe('abc')
  })
  it('обрезает длинные строки с многоточием', () => {
    expect(truncate('abcdefghij', 5)).toBe('abcde…')
  })
})

describe('relativeTime', () => {
  it('возвращает «только что» для свежих значений', () => {
    expect(relativeTime(new Date().toISOString())).toBe('только что')
  })
  it('возвращает «никогда» для пустого значения', () => {
    expect(relativeTime(null)).toBe('никогда')
  })
  it('считает часы и дни', () => {
    const hoursAgo = new Date(Date.now() - 5 * 3600_000).toISOString()
    expect(relativeTime(hoursAgo)).toBe('5 ч назад')
    const daysAgo = new Date(Date.now() - 3 * 86400_000).toISOString()
    expect(relativeTime(daysAgo)).toBe('3 дн назад')
  })
})

describe('statusTone', () => {
  it('успешные статусы — ok', () => {
    for (const status of ['published', 'succeeded', 'verified', 'online']) {
      expect(statusTone(status)).toBe('ok')
    }
  })
  it('ошибочные статусы — bad', () => {
    for (const status of ['failed', 'revoked', 'unhealthy', 'expired', 'skipped']) {
      expect(statusTone(status)).toBe('bad')
    }
  })
  it('rejected — idle (осознанный дизайн: не ошибка системы)', () => {
    expect(statusTone('rejected')).toBe('idle')
  })
  it('неизвестный статус — idle', () => {
    expect(statusTone('nope')).toBe('idle')
  })
})
