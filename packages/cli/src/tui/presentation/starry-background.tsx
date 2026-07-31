/** 首页星空与流星背景：可固定随机源测试，并在窄终端自动停用。 */

import { RGBA, StyledText, type TextChunk } from "@opentui/core"
import { useEffect, useMemo, useState } from "react"

import { tuiTheme } from "./theme"
import { blendRgba } from "./colors"

/**
 * 本组件根据 MiMo-Code 的 MIT 星空算法改写为 React/OpenTUI 版本。
 * 仅复用流星的子像素绘制思路，颜色和品牌资产均由 Harness Code 自行定义。
 * Copyright (c) 2026 MiMo Code, Xiaomi Corporation; Copyright (c) 2025 opencode.
 */

export const STAR_DENSITY = 0.00394
export const TWINKLE_INTERVAL_MS = 200
export const METEOR_INTERVAL_MS = 8000
export const METEOR_DURATION_MS = 3600
export const METEOR_FRAME_INTERVAL_MS = 50
export const METEOR_ANGLE = 0.36
export const METEOR_TAIL = 32
export const METEOR_STEP = 0.15

const STAR_CHARS = ["✦", "✧", "✦", "✧", "✦", "✧", "✦", " "]
const HOT_CHAR = "✶"
const HOT_THRESHOLD = 0.88

export type RandomSource = () => number

export type StarField = {
  grid: string[][]
  brightness: number[][]
}

export type Meteor = {
  at: number
  startX: number
  startY: number
  speed: number
}

type MeteorCell = {
  dots: number
  minT: number
}

/** 生成稳定的星点拓扑；亮度会由后续定时器单独更新，避免背景跳动。 */
export function createStarField(width: number, height: number, random: RandomSource = Math.random): StarField {
  const grid: string[][] = []
  const brightness: number[][] = []
  for (let y = 0; y < Math.max(1, height); y++) {
    const row: string[] = []
    const brightnessRow: number[] = []
    for (let x = 0; x < Math.max(1, width); x++) {
      if (random() < STAR_DENSITY) {
        row.push(String(Math.floor(random() * (STAR_CHARS.length - 1))))
        brightnessRow.push(0.15 + random() * 0.4)
      } else {
        row.push(" ")
        brightnessRow.push(0)
      }
    }
    grid.push(row)
    brightness.push(brightnessRow)
  }
  return { grid, brightness }
}

/** 每次只刷新少量已存在星点，使闪烁有节奏但不会变成噪点动画。 */
export function twinkleStarField(field: StarField, random: RandomSource = Math.random): StarField {
  const next: StarField = {
    grid: field.grid,
    brightness: field.brightness.map(row => [...row]),
  }
  const count = Math.floor(field.grid.length * (field.grid[0]?.length ?? 0) * 0.008)
  for (let index = 0; index < count; index++) {
    const y = Math.floor(random() * field.grid.length)
    const x = Math.floor(random() * (field.grid[0]?.length ?? 1))
    if (next.grid[y]?.[x] === " ") continue
    const value = random()
    next.brightness[y]![x] = value < 0.12
      ? 0.92 + random() * 0.08
      : value < 0.8
        ? 0.7 + random() * 0.22
        : 0.05 + random() * 0.2
  }
  return next
}

/** 生成从屏幕右上方进入的流星初始位置和限速，确保不同终端高度下时长稳定。 */
export function createMeteor(width: number, height: number, now: number, random: RandomSource = Math.random): Meteor {
  const startY = Math.floor(random() * 2)
  const speed = Math.max(0.011, Math.min(0.038, (height - startY) / (Math.sin(METEOR_ANGLE) * METEOR_DURATION_MS)))
  return {
    at: now,
    startX: width - random() * Math.max(1, width * 0.15),
    startY,
    speed,
  }
}

/** 将 2×4 Braille 子像素坐标映射到 Unicode Braille bit，保证斜线角度不被终端字符网格拉直。 */
export function brailleBit(column: number, row: number): number {
  if (column === 0) return row === 3 ? 6 : row
  return row === 3 ? 7 : 3 + row
}

/** 公开流星栅格便于单测检查尾迹、头部和边界裁剪，而不耦合终端渲染器。 */
export function createMeteorCells(field: StarField, meteor: Meteor | undefined, now: number): Map<string, MeteorCell> {
  const cells = new Map<string, MeteorCell>()
  if (!meteor) return cells
  const elapsed = now - meteor.at
  if (elapsed < 0 || elapsed > METEOR_DURATION_MS) return cells

  const width = field.grid[0]?.length ?? 0
  const height = field.grid.length
  const distance = elapsed * meteor.speed
  const dx = -Math.cos(METEOR_ANGLE)
  const dy = Math.sin(METEOR_ANGLE)
  const headX = meteor.startX + distance * dx
  const headY = meteor.startY + distance * dy

  const setDot = (pixelX: number, pixelY: number, tailDistance: number) => {
    const subX = Math.floor(pixelX * 2)
    const subY = Math.floor(pixelY * 4)
    const cellX = subX >> 1
    const cellY = subY >> 2
    if (cellX < 0 || cellX >= width || cellY < 0 || cellY >= height) return
    const key = `${cellX},${cellY}`
    const current = cells.get(key)
    cells.set(key, {
      dots: (current?.dots ?? 0) | (1 << brailleBit(subX & 1, subY & 3)),
      minT: Math.min(current?.minT ?? Infinity, tailDistance),
    })
  }

  for (let tail = 0; tail <= METEOR_TAIL; tail += METEOR_STEP) {
    setDot(headX - tail * dx, headY - tail * dy, tail)
  }

  // Braille 的 2×4 子像素接近视觉正方形，使用小圆盘可令流星头比单点更明亮清晰。
  const headSubX = Math.floor(headX * 2)
  const headSubY = Math.floor(headY * 4)
  for (let offsetX = -1; offsetX <= 1; offsetX++) {
    for (let offsetY = -1; offsetY <= 1; offsetY++) {
      if (offsetX * offsetX + offsetY * offsetY > 1) continue
      setDot((headSubX + offsetX) / 2, (headSubY + offsetY) / 4, 0)
    }
  }
  return cells
}

/** 将星点亮度和 Braille 流星轨迹合成为 OpenTUI StyledText。 */
export function renderStarField(field: StarField, meteor: Meteor | undefined, now: number): StyledText {
  const meteorCells = createMeteorCells(field, meteor, now)
  const elapsed = meteor ? now - meteor.at : 0
  const envelope = meteor && elapsed >= 0 && elapsed <= METEOR_DURATION_MS
    ? Math.sin((elapsed / METEOR_DURATION_MS) * Math.PI)
    : 0
  const chunks: TextChunk[] = []
  const background = RGBA.fromHex(tuiTheme.background)
  const starBase = RGBA.fromHex(tuiTheme.star)
  const hotStar = RGBA.fromHex(tuiTheme.trail)
  const glow = RGBA.fromHex(tuiTheme.primary)
  const core = RGBA.fromInts(235, 244, 255)

  field.grid.forEach((row, y) => {
    row.forEach((cell, x) => {
      const meteorCell = meteorCells.get(`${x},${y}`)
      if (meteorCell) {
        const fade = Math.pow(1 - meteorCell.minT / METEOR_TAIL, 1.3) * envelope
        const headBlend = Math.max(0, 1 - meteorCell.minT / 5)
        appendChunk(chunks, String.fromCharCode(0x2800 + meteorCell.dots), blendRgba(background, blendRgba(glow, core, headBlend), Math.max(0.02, fade)))
        return
      }

      const brightness = field.brightness[y]?.[x] ?? 0
      if (cell === " " || brightness === 0) {
        appendChunk(chunks, " ", background)
        return
      }
      const hot = brightness >= HOT_THRESHOLD
      const peak = hot ? Math.min(1, (brightness - HOT_THRESHOLD) / (1 - HOT_THRESHOLD)) : 0
      const starColor = peak > 0
        ? blendRgba(blendRgba(background, starBase, Math.min(1, brightness * 1.1)), hotStar, peak * 0.7)
        : blendRgba(background, starBase, Math.min(1, brightness * 1.1))
      appendChunk(chunks, hot ? HOT_CHAR : STAR_CHARS[Number(cell) % (STAR_CHARS.length - 1)]!, starColor)
    })
    if (y < field.grid.length - 1) chunks.push({ __isChunk: true, text: "\n", attributes: 0 })
  })
  return new StyledText(chunks)
}

/** React/OpenTUI 背景组件：负责尺寸变化、星点闪烁和流星定时器生命周期。 */
export function StarryBackground(props: { width: number; height: number }) {
  const [field, setField] = useState(() => createStarField(props.width, props.height))
  const [meteor, setMeteor] = useState<Meteor>()
  const [now, setNow] = useState(() => performance.now())

  useEffect(() => {
    setField(createStarField(props.width, props.height))
  }, [props.width, props.height])

  useEffect(() => {
    const timer = setInterval(() => setField(current => twinkleStarField(current)), TWINKLE_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    let frameTimer: ReturnType<typeof setInterval> | undefined
    const meteorTimer = setInterval(() => {
      const startedAt = performance.now()
      setMeteor(createMeteor(props.width, props.height, startedAt))
      setNow(startedAt)
      if (frameTimer) clearInterval(frameTimer)
      frameTimer = setInterval(() => {
        const current = performance.now()
        setNow(current)
        if (current - startedAt > METEOR_DURATION_MS) {
          clearInterval(frameTimer)
          frameTimer = undefined
          setMeteor(undefined)
        }
      }, METEOR_FRAME_INTERVAL_MS)
    }, METEOR_INTERVAL_MS)
    return () => {
      clearInterval(meteorTimer)
      if (frameTimer) clearInterval(frameTimer)
    }
  }, [props.height, props.width])

  const content = useMemo(() => renderStarField(field, meteor, now), [field, meteor, now])
  return (
    <box position="absolute" top={0} left={0} width="100%" height="100%" zIndex={0}>
      <text content={content} width="100%" height="100%" wrapMode="none" selectable={false} />
    </box>
  )
}

/** 合并相邻同色文本块，减少终端绘制对象数量。 */
function appendChunk(chunks: TextChunk[], text: string, fg: RGBA) {
  const previous = chunks.at(-1)
  if (previous?.fg instanceof RGBA && previous.fg.equals(fg) && previous.bg === undefined && previous.attributes === 0) {
    previous.text += text
    return
  }
  chunks.push({ __isChunk: true, text, fg, attributes: 0 })
}
