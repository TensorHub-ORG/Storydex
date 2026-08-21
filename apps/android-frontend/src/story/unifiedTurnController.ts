import type { KeywordLibrary } from '@/stores/keywordLibraries'
import type { ProjectSettings } from '@/stores/project'
import type { AgentMode } from '@/stores/story'
import {
  buildDirectorPlan,
  stableTurnSeed,
  type DirectorPlan,
  type DirectorState,
} from './directorMechanics'
import {
  rollMechanics,
  type CharacterGenderMode,
  type EncounterFrequency,
  type MechanicsRollResult,
} from './randomMechanics'

export interface UnifiedTurnLibraries {
  event: KeywordLibrary
  male: KeywordLibrary
  female: KeywordLibrary
  tragedy: KeywordLibrary
  payoff: KeywordLibrary
}

export interface UnifiedTurnOptions {
  agentMode: AgentMode
  directorState: DirectorState
  settings: Pick<
    ProjectSettings,
    'directorEnabled' | 'storyPace' | 'majorHookEnabled' | 'stagnationWarningThreshold' | 'plotMechanics'
  >
  primaryScriptFocus?: DirectorPlan['scriptFocus']
  fortuneEnabled: boolean
  encounterEnabled: boolean
  encounterFrequency: EncounterFrequency
  eventEnabled: boolean
  characterEnabled: boolean
  characterGender: CharacterGenderMode
  tragedyEnabled: boolean
  payoffEnabled: boolean
  libraries: UnifiedTurnLibraries
}

export interface UnifiedTurnPreparation {
  turnId: string
  randomSeed: number
  directorPlan: DirectorPlan | null
  mechanics: MechanicsRollResult
}

/**
 * Single authority for all pre-generation decisions. Director, script focus,
 * random mechanics and retry seed are frozen together before prompt assembly.
 */
export function prepareUnifiedTurn(options: UnifiedTurnOptions): UnifiedTurnPreparation {
  const isStory = options.agentMode === 'story'
  const directorPlan = isStory && options.settings.directorEnabled
    ? buildDirectorPlan(
      options.directorState,
      options.settings.storyPace,
      options.settings.majorHookEnabled,
      options.primaryScriptFocus,
      options.settings.stagnationWarningThreshold,
      options.settings.plotMechanics,
    )
    : null
  const randomSeed = directorPlan?.randomSeed ?? stableTurnSeed(
    options.directorState.revision,
    options.directorState.turnIndex + 1,
    options.primaryScriptFocus?.id ?? '',
  )
  const mechanics = rollMechanics({
    fortuneEnabled: isStory && options.fortuneEnabled,
    encounterEnabled: isStory && options.encounterEnabled,
    encounterFrequency: options.encounterFrequency,
    eventEnabled: options.eventEnabled,
    characterEnabled: options.characterEnabled,
    characterGender: options.characterGender,
    tragedyEnabled: options.tragedyEnabled,
    payoffEnabled: options.payoffEnabled,
    eventLibrary: options.libraries.event,
    maleLibrary: options.libraries.male,
    femaleLibrary: options.libraries.female,
    tragedyLibrary: options.libraries.tragedy,
    payoffLibrary: options.libraries.payoff,
    allowedKinds: directorPlan?.allowedEncounterKinds,
    progressionAction: directorPlan?.action,
    randomSeed,
  })
  if (directorPlan && mechanics.encounter?.triggered && mechanics.encounter.primary) {
    directorPlan.encounterKind = mechanics.encounter.primary
    directorPlan.encounterIntensity = mechanics.encounter.intensity
  }
  return {
    turnId: directorPlan?.control.turnId ?? `turn-${options.directorState.revision}-${options.directorState.turnIndex + 1}`,
    randomSeed,
    directorPlan,
    mechanics,
  }
}
