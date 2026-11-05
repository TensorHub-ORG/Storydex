<template>
  <section
    class="story-state-panel"
    :class="{ 'is-loading': panelLoading, 'is-expanded': expanded, 'is-relationship-only': relationshipOnly }"
  >
    <header class="ssp-header">
      <div class="ssp-title">
        <span class="material-symbols-rounded ssp-title-icon">{{ relationshipOnly ? "hub" : "timeline" }}</span>
        <span class="ssp-title-text">
          <span class="ssp-title-main">{{ relationshipOnly ? "知识图谱" : "项目状态" }}</span>
          <small v-if="relationshipOnly && wikiHeaderMeta" class="ssp-title-meta" :title="wikiHeaderTooltip">{{ wikiHeaderMeta }}</small>
        </span>
      </div>
      <div class="ssp-header-actions">
        <button
          v-if="relationshipOnly"
          class="ssp-refresh ssp-rebuild"
          type="button"
          :disabled="panelLoading"
          title="Agent 深度生成知识图谱"
          @click="runWikiAgentWorkflow('generate')"
        >
          <span class="material-symbols-rounded">auto_awesome</span>
          <span class="ssp-action-label">Agent 生成</span>
        </button>
        <button
          v-if="relationshipOnly"
          class="ssp-refresh"
          type="button"
          :disabled="panelLoading"
          title="Agent 增量更新 WIKI"
          @click="runWikiAgentWorkflow('update')"
        >
          <span class="material-symbols-rounded">published_with_changes</span>
          <span class="ssp-action-label">增量更新</span>
        </button>
        <button
          v-if="relationshipOnly"
          class="ssp-refresh"
          type="button"
          :disabled="panelLoading"
          title="Agent 审阅 WIKI"
          @click="runWikiAgentWorkflow('review')"
        >
          <span class="material-symbols-rounded">fact_check</span>
          <span class="ssp-action-label">审阅</span>
        </button>
        <span v-if="relationshipOnly" class="ssp-header-divider" aria-hidden="true"></span>
        <button
          class="ssp-refresh"
          type="button"
          :disabled="panelLoading"
          :title="panelLoading ? '加载中...' : '刷新'"
          @click="refreshPanel"
        >
          <span class="material-symbols-rounded">refresh</span>
        </button>
      </div>
    </header>

    <nav v-if="!relationshipOnly" class="ssp-tabs" aria-label="项目状态分类">
      <button
        v-for="tab in visibleTabs"
        :key="tab.id"
        class="ssp-tab"
        :class="{ active: activeTab === tab.id }"
        type="button"
        @click="activeTab = tab.id"
      >
        <span>{{ tab.label }}</span>
        <span v-if="tab.count > 0" class="ssp-tab-count">{{ tab.count }}</span>
      </button>
    </nav>

    <div class="ssp-body">
      <div v-if="errorMessage || (!relationshipOnly && wikiErrorMessage)" class="ssp-empty">⚠ {{ errorMessage || wikiErrorMessage }}</div>

      <div
        v-else-if="relationshipOnly"
        class="ssp-wiki-workspace"
        :class="{ 'is-inspector-open': wikiInspectorOpen }"
      >
        <section class="ssp-wiki-toolbar">
          <nav class="ssp-wiki-category-tabs" role="tablist" aria-label="知识图谱分类">
            <button
              v-for="category in wikiCategoryTabs"
              :key="category.id"
              class="ssp-wiki-category-tab"
              :class="{ active: selectedWikiCategory === category.id }"
              type="button"
              role="tab"
              :aria-selected="selectedWikiCategory === category.id"
              @click="selectWikiCategory(category.id)"
            >
              <span class="material-symbols-rounded">{{ category.icon }}</span>
              <span class="ssp-wiki-category-name">{{ category.label }}</span>
              <small v-if="category.count > 0">{{ category.count }}</small>
            </button>
          </nav>
          <form class="ssp-wiki-search-form" @submit.prevent="submitWikiGraphSearch">
            <span class="material-symbols-rounded">search</span>
            <input
              v-model="wikiGraphSearchInput"
              class="ssp-wiki-search-input"
              type="search"
              placeholder="搜索条目、节点或关系"
            />
            <button
              v-if="wikiGraphSearchQuery || wikiGraphSearchInput"
              class="ssp-wiki-search-clear"
              type="button"
              title="清除搜索"
              @click="clearWikiGraphSearch"
            >
              <span class="material-symbols-rounded">close</span>
            </button>
          </form>
          <div class="ssp-wiki-toolbar-actions">
            <button
              class="ssp-wiki-review-alert"
              :class="{ 'has-pending': wikiNeedsReviewCount > 0, active: wikiReviewOpen }"
              type="button"
              title="查看并处理待确认的关系"
              @click="toggleWikiReviewQueue"
            >
              <span class="material-symbols-rounded">pending_actions</span>
              <span class="ssp-wiki-review-alert-label">关系待确认</span>
              <strong class="ssp-wiki-review-alert-count">{{ wikiNeedsReviewCount }}</strong>
            </button>
            <button
              class="ssp-wiki-inspector-toggle"
              :class="{ active: wikiInspectorOpen }"
              type="button"
              :title="wikiInspectorOpen ? '收起详情' : '展开详情'"
              @click="wikiInspectorOpen = !wikiInspectorOpen"
            >
              <span class="material-symbols-rounded">right_panel_open</span>
            </button>
          </div>
        </section>

        <div v-if="wikiAgentStatus" class="ssp-wiki-agent-status" :class="wikiAgentTone">
          <span class="material-symbols-rounded">{{ wikiAgentTone === 'warning' || wikiAgentTone === 'error' ? 'error' : 'info' }}</span>
          <span class="ssp-wiki-agent-status-text">{{ wikiAgentStatus }}</span>
          <button class="ssp-wiki-agent-status-close" type="button" title="关闭提示" @click="wikiAgentStatus = ''">
            <span class="material-symbols-rounded">close</span>
          </button>
        </div>

        <main class="ssp-wiki-main">
          <section
            ref="wikiGraphPanelRef"
            class="ssp-wiki-graph-panel"
            :class="{ 'is-busy': wikiGraphLoading && wikiGraphNodes.length > 0 }"
          >
            <div v-if="(wikiLoading || wikiGraphLoading) && !wikiGraphNodes.length" class="ssp-wiki-graph-empty">
              <span class="material-symbols-rounded is-spinning">progress_activity</span>
              <p>正在读取知识图谱…</p>
            </div>
            <template v-else-if="wikiGraphNodes.length">
              <svg
                ref="wikiGraphSvgRef"
                class="ssp-wiki-graph"
                :viewBox="`0 0 ${wikiCanvasSize.width} ${wikiCanvasSize.height}`"
                role="img"
                aria-label="Storydex knowledge graph"
                @wheel.prevent="handleWikiGraphWheel"
                @pointerdown.self="beginWikiGraphPan"
                @pointermove="moveWikiGraphPointer"
                @pointerup="endWikiGraphPointer"
                @pointercancel="endWikiGraphPointer"
                @click.self="handleWikiGraphBackgroundClick"
              >
                <g :transform="`translate(${wikiGraphPan.x} ${wikiGraphPan.y}) scale(${wikiGraphZoom})`">
                  <g
                    v-for="caption in wikiLayoutCaptions"
                    :key="`caption-${caption.band}`"
                    class="ssp-wiki-band-caption"
                    :transform="`translate(${caption.x} ${caption.y})`"
                  >
                    <text x="0" y="0" dominant-baseline="central">{{ caption.band === 'spine' ? '章节顺序' : '独立条目' }} · {{ caption.count }}</text>
                    <line :x1="78 + String(caption.count).length * 7" y1="0" :x2="caption.width" y2="0" />
                    <title>{{ caption.band === 'spine' ? '章节按叙事顺序排列' : '在当前视图中没有连线的条目' }}</title>
                  </g>
                  <g
                    v-for="edge in wikiGraphEdges"
                    :key="edge.id"
                    class="ssp-wiki-edge-group"
                    :class="{ dimmed: isWikiEdgeDimmed(edge), hovered: hoveredWikiEdgeId === edge.id }"
                    :style="{ '--wiki-edge-width': edge.strokeWidth }"
                    @click.stop="selectWikiEdge(edge.id)"
                    @pointerenter="hoveredWikiEdgeId = edge.id"
                    @pointerleave="hoveredWikiEdgeId = ''"
                  >
                    <path
                      class="ssp-wiki-edge"
                      :class="[`edge-${edge.type}`, {
                        active: edge.active,
                        dimmed: isWikiEdgeDimmed(edge),
                        'edge-co-occurrence': edge.coOccurrence,
                        'edge-real-relation': edge.realRelation,
                        'edge-review-required': edge.reviewStatus === 'review_required',
                      }]"
                      :d="edge.pathD"
                    />
                    <path v-if="edge.arrowD" class="ssp-wiki-edge-arrow" :d="edge.arrowD" />
                    <path class="ssp-wiki-edge-hit" :d="edge.pathD" />
                  </g>
                  <g
                    v-for="node in wikiGraphNodes"
                    :key="node.id"
                    class="ssp-wiki-node"
                    :class="[
                      `node-${node.type}`,
                      `tone-${node.tone}`,
                      `band-${node.band}`,
                      {
                        active: isWikiNodeSelectable(node) && selectedWikiNodeId === node.id,
                        dimmed: isWikiNodeDimmed(node),
                        'is-neighbor': node.neighbor,
                        'is-synthetic': node.synthetic,
                        'is-selectable': isWikiNodeSelectable(node),
                        'needs-review': node.needsReview,
                      },
                    ]"
                    :transform="`translate(${node.x} ${node.y})`"
                    @pointerdown.stop="beginWikiNodeDrag($event, node)"
                    @pointerenter="hoveredWikiNodeId = node.id"
                    @pointerleave="hoveredWikiNodeId = ''"
                    @click.stop="selectWikiNode(node)"
                  >
                    <circle class="ssp-wiki-node-halo" :r="node.radius + 5" />
                    <circle class="ssp-wiki-node-dot" :r="node.radius" />
                    <text
                      class="ssp-wiki-node-glyph"
                      text-anchor="middle"
                      dominant-baseline="central"
                      :style="{ fontSize: `${Math.round(node.radius * 1.02)}px` }"
                    >{{ node.glyph }}</text>
                    <circle
                      v-if="node.needsReview"
                      class="ssp-wiki-node-badge"
                      :cx="node.radius * 0.72"
                      :cy="-node.radius * 0.72"
                      r="4"
                    />
                    <text
                      class="ssp-wiki-node-label"
                      :x="wikiNodeLabelLayout[node.id]?.dx ?? 0"
                      :y="wikiNodeLabelLayout[node.id]?.dy ?? node.radius + 12"
                      :text-anchor="wikiNodeLabelLayout[node.id]?.anchor ?? 'middle'"
                      dominant-baseline="central"
                    >{{ node.shortLabel }}</text>
                    <title>{{ node.label }}</title>
                  </g>
                  <g
                    v-for="edge in visibleWikiGraphLabelEdges"
                    :key="`${edge.id}-label`"
                    class="ssp-wiki-edge-label"
                    :class="{
                      active: edge.active,
                      hovered: hoveredWikiEdgeId === edge.id,
                      'is-review': edge.reviewStatus === 'review_required',
                    }"
                    :transform="`translate(${edge.labelX} ${edge.labelY})`"
                    @click.stop="selectWikiEdge(edge.id)"
                    @pointerenter="hoveredWikiEdgeId = edge.id"
                    @pointerleave="hoveredWikiEdgeId = ''"
                  >
                    <rect
                      :x="-edge.labelWidth / 2"
                      :y="-9 * wikiLabelScale"
                      :width="edge.labelWidth"
                      :height="18 * wikiLabelScale"
                      :rx="9 * wikiLabelScale"
                    />
                    <text text-anchor="middle" dominant-baseline="central">{{ edge.displayLabel }}</text>
                  </g>
                </g>
              </svg>
              <div v-if="wikiGraphHasMore" class="ssp-wiki-graph-note">
                <span>已显示 {{ wikiGraphStats.returnedNodeCount }} / {{ wikiGraphStats.nodeCount }} 个节点</span>
                <button type="button" :disabled="wikiGraphLoading" @click="loadMoreWikiGraph">继续加载</button>
              </div>
              <div v-if="wikiErrorMessage" class="ssp-wiki-graph-error">
                <span class="material-symbols-rounded">error</span>
                <span>{{ wikiErrorMessage }}</span>
              </div>
              <div v-if="wikiGraphLegend.length || wikiEdgeLegend.length" class="ssp-wiki-graph-legend">
                <span v-for="item in wikiGraphLegend" :key="item.key" class="ssp-wiki-legend-item">
                  <i :class="`tone-${item.key}`"></i>{{ item.label }}
                </span>
                <span v-if="wikiGraphLegend.length && wikiEdgeLegend.length" class="ssp-wiki-legend-divider" aria-hidden="true"></span>
                <span
                  v-for="item in wikiEdgeLegend"
                  :key="item.key"
                  class="ssp-wiki-legend-item ssp-wiki-legend-edge"
                  :class="`edge-${item.key}`"
                >
                  <svg viewBox="0 0 18 6" aria-hidden="true"><line x1="1" y1="3" x2="17" y2="3" /></svg>
                  <span>{{ item.count === null ? item.label : `${item.label} ${item.count}` }}</span>
                </span>
              </div>
              <div class="ssp-wiki-graph-hud">
                <button class="ssp-hud-btn" type="button" title="缩小" @click="zoomWikiGraphStep(-1)">
                  <span class="material-symbols-rounded">remove</span>
                </button>
                <span class="ssp-hud-zoom">{{ Math.round(wikiGraphZoom * 100) }}%</span>
                <button class="ssp-hud-btn" type="button" title="放大" @click="zoomWikiGraphStep(1)">
                  <span class="material-symbols-rounded">add</span>
                </button>
                <span class="ssp-hud-divider" aria-hidden="true"></span>
                <button class="ssp-hud-btn" type="button" title="重新布局并适配视图" @click="resetWikiGraphView">
                  <span class="material-symbols-rounded">fit_screen</span>
                </button>
              </div>
            </template>
            <div v-else class="ssp-wiki-graph-empty" :class="{ 'is-error': !!wikiErrorMessage }">
              <span class="material-symbols-rounded">{{ wikiErrorMessage ? 'cloud_off' : 'hub' }}</span>
              <p>{{ wikiErrorMessage || '暂无知识图谱数据' }}</p>
              <small v-if="!wikiErrorMessage">点击右上角「Agent 生成」让 Agent 深度整理，或保存正文后自动同步。</small>
              <button v-else class="ssp-wiki-empty-action" type="button" @click="refreshPanel">
                <span class="material-symbols-rounded">refresh</span>重试
              </button>
            </div>
          </section>

          <aside ref="wikiInspectorRef" class="ssp-wiki-inspector" :class="{ 'is-open': wikiInspectorOpen }">
            <button class="ssp-wiki-inspector-close" type="button" title="收起详情" @click="wikiInspectorOpen = false">
              <span class="material-symbols-rounded">close</span>
            </button>
            <div class="ssp-wiki-inspector-body">
              <article v-if="selectedWikiRelationEdge" class="ssp-wiki-inspector-detail">
                <div class="ssp-wiki-detail-kicker-row">
                  <span class="ssp-wiki-entry-kicker">
                    {{ selectedWikiRelationEdge.coOccurrence ? "章节共现" : selectedWikiRelationEdge.structural ? "章节顺序" : "知识关系" }}
                  </span>
                  <span
                    v-if="!selectedWikiRelationEdge.structural"
                    class="ssp-wiki-status-chip"
                    :class="`status-${selectedWikiRelationEdge.reviewStatus}`"
                  >{{ wikiReviewStatusLabel(selectedWikiRelationEdge.reviewStatus) }}</span>
                </div>
                <h3>{{ selectedWikiRelationEdge.label }}</h3>
                <p
                  v-if="selectedWikiRelationEdge.semanticsLabel && selectedWikiRelationEdge.semanticsLabel !== selectedWikiRelationEdge.label"
                  class="ssp-wiki-detail-subtitle"
                >{{ selectedWikiRelationEdge.semanticsLabel }}</p>
                <div class="ssp-wiki-relation-endpoints">
                  <button
                    type="button"
                    class="ssp-wiki-endpoint"
                    :class="`tone-${selectedWikiRelationEdge.source.tone}`"
                    @click="selectWikiNode(selectedWikiRelationEdge.source)"
                  ><i></i>{{ selectedWikiRelationEdge.source.label }}</button>
                  <span class="material-symbols-rounded">{{ selectedWikiRelationEdge.realRelation || selectedWikiRelationEdge.structural ? 'arrow_forward' : 'sync_alt' }}</span>
                  <button
                    type="button"
                    class="ssp-wiki-endpoint"
                    :class="`tone-${selectedWikiRelationEdge.target.tone}`"
                    @click="selectWikiNode(selectedWikiRelationEdge.target)"
                  ><i></i>{{ selectedWikiRelationEdge.target.label }}</button>
                </div>
                <div v-if="selectedWikiRelationEdge.level !== null" class="ssp-wiki-relation-level">
                  <span>强度 {{ selectedWikiRelationEdge.level }}</span>
                  <div class="ssp-level-bar">
                    <div
                      class="ssp-level-fill"
                      :style="{ width: levelWidth(selectedWikiRelationEdge.level), background: levelColor(selectedWikiRelationEdge.level) }"
                    ></div>
                  </div>
                </div>
                <p v-if="selectedWikiRelationEdge.coOccurrence">
                  此连线仅表示两角色在同一章节被共同提及，不代表既定关系。
                  <span v-if="selectedWikiRelationEdge.evidence" class="ssp-wiki-evidence-note">共现章节：{{ selectedWikiRelationEdge.evidence }}</span>
                </p>
                <p v-else-if="selectedWikiRelationEdge.structural">章节按叙事顺序自动连接。</p>
                <p v-else-if="!selectedWikiRelationEdge.sourceRefs.length">{{ selectedWikiRelationEdge.evidence || "该关系来自知识图谱，暂未附带原文引文。" }}</p>
                <div v-if="selectedWikiRelationEdge.sourceRefs.length" class="ssp-wiki-source-ref-list">
                  <div class="ssp-wiki-section-label">原文证据</div>
                  <div v-for="(sourceRef, sourceIndex) in selectedWikiRelationEdge.sourceRefs" :key="`${sourceRef.path}-${sourceIndex}`" class="ssp-wiki-source-ref">
                    <blockquote>{{ sourceRef.quote }}</blockquote>
                    <button type="button" class="ssp-wiki-source-link" @click="openWikiSource(sourceRef.path)">
                      <span class="material-symbols-rounded">description</span>
                      <span>{{ sourceRef.path }}</span>
                    </button>
                  </div>
                </div>
                <small v-if="!selectedWikiRelationEdge.structural">引文匹配只说明原文存在；关系含义、称谓与推断仍需结合上下文核对。</small>
                <div v-if="selectedWikiReviewCandidate" class="ssp-wiki-review-inline-actions">
                  <button
                    class="ssp-btn accept"
                    type="button"
                    :disabled="wikiReviewActionId === selectedWikiReviewCandidate.id"
                    @click="confirmWikiCandidate(selectedWikiReviewCandidate)"
                  >确认</button>
                  <button
                    class="ssp-btn dismiss"
                    type="button"
                    :disabled="wikiReviewActionId === selectedWikiReviewCandidate.id"
                    @click="toggleWikiReviewQueue"
                  >查看审核队列</button>
                </div>
              </article>

              <article v-else-if="selectedWikiNode || selectedWikiDetailEntry" class="ssp-wiki-inspector-detail">
                <div class="ssp-wiki-detail-kicker-row">
                  <span
                    class="ssp-wiki-entry-kicker"
                    :class="`tone-${selectedWikiNode?.tone || (selectedWikiDetailEntry ? wikiEntryTone(selectedWikiDetailEntry) : 'misc')}`"
                  ><i></i>{{ selectedWikiDetailKicker }}</span>
                  <span v-if="selectedWikiDetailEntry?.needsReview" class="ssp-wiki-review-chip">需要人工确认</span>
                </div>
                <h3>{{ selectedWikiNode?.label || selectedWikiDetailEntry?.title }}</h3>
                <p>{{ selectedWikiDetailSummary || "暂无摘要。" }}</p>
                <ul v-if="selectedWikiDetailLines.length" class="ssp-wiki-detail-lines">
                  <li v-for="(detail, index) in selectedWikiDetailLines" :key="index">{{ detail }}</li>
                </ul>
                <section v-if="wikiFocusRelations.length" class="ssp-wiki-detail-section">
                  <div class="ssp-wiki-section-label">关系 · {{ wikiFocusRelations.length }}</div>
                  <button
                    v-for="relation in wikiFocusRelations"
                    :key="relation.id"
                    type="button"
                    class="ssp-wiki-relation-row"
                    :class="{ 'is-review': relation.needsReview }"
                    @click="selectWikiEdge(relation.id)"
                  >
                    <span class="material-symbols-rounded">{{ relation.outgoing ? 'arrow_forward' : 'arrow_back' }}</span>
                    <span class="ssp-wiki-relation-other" :class="`tone-${relation.otherTone}`"><i></i>{{ relation.otherLabel }}</span>
                    <span class="ssp-wiki-relation-predicate">
                      {{ relation.category }}<small v-if="relation.predicate && relation.predicate !== relation.category"> · {{ relation.predicate }}</small>
                    </span>
                    <span v-if="relation.needsReview" class="ssp-wiki-status-chip status-review_required">待确认</span>
                  </button>
                </section>
                <section v-if="selectedWikiDetailEntry?.sourcePaths?.length" class="ssp-wiki-detail-section">
                  <div class="ssp-wiki-section-label">来源</div>
                  <div class="ssp-wiki-sources">
                    <button
                      v-for="source in selectedWikiDetailEntry.sourcePaths.slice(0, 8)"
                      :key="source"
                      type="button"
                      class="ssp-wiki-source-chip"
                      :title="`打开 ${source}`"
                      @click="openWikiSource(source)"
                    >
                      <span class="material-symbols-rounded">description</span>
                      <span>{{ source }}</span>
                    </button>
                  </div>
                </section>
              </article>

              <article v-else class="ssp-wiki-inspector-detail is-overview">
                <div class="ssp-wiki-detail-kicker-row">
                  <span class="ssp-wiki-entry-kicker">
                    <span class="material-symbols-rounded">travel_explore</span>{{ wikiData?.projectName ? "项目概览" : wikiInspectorEmptyTitle }}
                  </span>
                </div>
                <h3>{{ wikiData?.projectName || wikiInspectorEmptyTitle }}</h3>
                <p v-if="wikiProjectSummary">{{ wikiProjectSummary }}</p>
                <p class="ssp-wiki-overview-hint">{{ wikiInspectorEmptyHint }}</p>
              </article>

              <section v-if="wikiReviewOpen" class="ssp-wiki-review-queue">
                <div class="ssp-wiki-inspector-heading">
                  <span>关系待确认 · {{ wikiReviewTotal }}</span>
                  <button type="button" class="ssp-wiki-review-refresh" @click="loadWikiReviewQueue">刷新</button>
                </div>
                <div v-if="wikiReviewLoading" class="ssp-wiki-review-empty">正在读取审核队列…</div>
                <div v-else-if="wikiReviewError" class="ssp-wiki-review-error">{{ wikiReviewError }}</div>
                <div v-else-if="!wikiReviewQueue.length" class="ssp-wiki-review-empty">暂无待确认关系。</div>
                <article v-for="candidate in wikiReviewQueue" :key="candidate.id" class="ssp-wiki-review-card">
                  <div class="ssp-wiki-review-card-title">
                    <strong>{{ wikiEndpointLabel(candidate.subjectId, candidate.subject) }}</strong>
                    <span class="material-symbols-rounded">arrow_forward</span>
                    <strong>{{ wikiEndpointLabel(candidate.objectId, candidate.object) }}</strong>
                    <span class="ssp-wiki-status-chip status-review_required">{{ wikiReviewStatusLabel(candidate.reviewStatus) }}</span>
                  </div>
                  <p v-if="candidate.reviewReason">{{ candidate.reviewReason }}</p>
                  <div v-for="(sourceRef, sourceIndex) in (candidate.sourceRefs || [])" :key="`${candidate.id}-source-${sourceIndex}`" class="ssp-wiki-source-ref">
                    <blockquote>{{ sourceRef.quote }}</blockquote>
                    <button type="button" class="ssp-wiki-source-link" @click="openWikiSource(sourceRef.path)">
                      <span class="material-symbols-rounded">description</span>
                      <span>打开来源：{{ sourceRef.path }}</span>
                    </button>
                  </div>
                  <label class="ssp-wiki-review-field">
                    谓词
                    <input v-model="wikiReviewDraft(candidate).predicate" type="text" />
                  </label>
                  <div class="ssp-wiki-review-field-row">
                    <label class="ssp-wiki-review-field">
                      主体
                      <select v-model="wikiReviewDraft(candidate).subjectId">
                        <option v-for="node in wikiEndpointOptions(candidate)" :key="node.id" :value="node.id">{{ node.label }}</option>
                      </select>
                    </label>
                    <label class="ssp-wiki-review-field">
                      客体
                      <select v-model="wikiReviewDraft(candidate).objectId">
                        <option v-for="node in wikiEndpointOptions(candidate)" :key="node.id" :value="node.id">{{ node.label }}</option>
                      </select>
                    </label>
                  </div>
                  <small>引文匹配只说明原文存在。请核对关系双方、称谓和推断是否成立；确认结果在图谱重建后保留。</small>
                  <div class="ssp-wiki-review-reject">
                    <label class="ssp-wiki-review-field">
                      驳回原因
                      <select v-model="wikiReviewDraft(candidate).rejectReason">
                        <option value="incorrect">关系错误</option>
                        <option value="ambiguous">证据有歧义</option>
                        <option value="duplicate">重复关系</option>
                        <option value="not_canon">未采纳的设定</option>
                        <option value="other">其他原因</option>
                      </select>
                    </label>
                    <label class="ssp-wiki-review-field">
                      驳回备注
                      <textarea v-model="wikiReviewDraft(candidate).rejectNote" rows="2" />
                    </label>
                  </div>
                  <div class="ssp-action-row">
                    <button class="ssp-btn accept" type="button" :disabled="wikiReviewActionId === candidate.id" @click="confirmWikiCandidate(candidate)">确认</button>
                    <button class="ssp-btn dismiss" type="button" :disabled="wikiReviewActionId === candidate.id" @click="rejectWikiCandidate(candidate)">驳回</button>
                  </div>
                  <small v-if="candidate.usesSidecar" class="ssp-wiki-sidecar-note">该关系将写入 JSON 角色卡 sidecar。</small>
                </article>
              </section>

              <section class="ssp-wiki-entry-list">
                <div class="ssp-wiki-inspector-heading">
                  <span>{{ wikiGraphSearchQuery ? "搜索结果" : `${selectedWikiCategoryLabel}条目` }} · {{ visibleWikiEntries.length }}</span>
                </div>
                <button
                  v-for="entry in visibleWikiEntries"
                  :key="entry.id"
                  class="ssp-wiki-entry-button"
                  :class="[`tone-${wikiEntryTone(entry)}`, { active: wikiActiveEntryId === entry.id }]"
                  type="button"
                  @click="selectWikiEntry(entry.id)"
                >
                  <i class="ssp-wiki-entry-dot" aria-hidden="true"></i>
                  <span class="ssp-wiki-entry-text">
                    <strong>{{ entry.title }}</strong>
                    <span>{{ cleanWikiSummary(entry.summary, entry.title) }}</span>
                  </span>
                  <small v-if="entry.needsReview" class="ssp-wiki-review-chip">需要人工确认</small>
                </button>
              </section>
            </div>
          </aside>
        </main>
      </div>

      <!-- 变更 ledger -->
      <div v-else-if="activeTab === 'changes'">
        <div v-if="changeEntries.length === 0" class="ssp-empty">暂无变更记录。</div>
        <ul v-else class="ssp-list">
          <li v-for="(entry, idx) in changeEntries" :key="idx" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ entry.chapter_id || '-' }} · {{ entry.segment_id || '-' }}</span>
              <span class="ssp-time">{{ formatTime(entry.at) }}</span>
            </div>
            <div v-if="entry.snapshot_comment" class="ssp-snapshot">{{ entry.snapshot_comment }}</div>
            <div class="ssp-sizes">
              <span v-for="(value, key) in entry.sizes" :key="key" v-show="value > 0" class="ssp-size-pill">
                {{ shortFieldLabel(String(key)) }}: {{ value }}
              </span>
            </div>
            <div v-if="entry.error_count" class="ssp-error">{{ entry.error_count }} 条 error</div>
            <div class="ssp-action-row">
              <button
                class="ssp-btn rollback"
                type="button"
                :disabled="rollingBack === idx || !entryHasSegmentPath(entry)"
                :title="entryHasSegmentPath(entry) ? '回档到该段之前（保留该段；后续段落与变量将备份后删除）' : '该条记录缺少可定位的 segment 路径'"
                @click="rollbackToEntry(entry, idx)"
              >
                <span class="material-symbols-rounded">undo</span>
                回档至此
              </button>
            </div>
          </li>
        </ul>
      </div>

      <!-- 章节大纲 -->
      <div v-else-if="activeTab === 'outline'">
        <div v-if="outlineChapters.length === 0" class="ssp-empty">暂无章节大纲。</div>
        <ul v-else class="ssp-list">
          <li v-for="chap in outlineChapters" :key="chap.chapter_id" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ chap.chapter_id }}</span>
              <span class="ssp-time">最近段: {{ chap.last_updated_in || '-' }}</span>
            </div>
            <div v-if="chap.arc_summary" class="ssp-snapshot">{{ chap.arc_summary }}</div>
            <div class="ssp-section-head">里程碑（{{ chap.milestones?.length || 0 }}）</div>
            <ol class="ssp-mini-list">
              <li v-for="(m, i) in (chap.milestones || []).slice(-5)" :key="i">
                <span class="ssp-mini-meta">[{{ m.segment_id }}]</span> {{ m.summary }}
              </li>
            </ol>
            <div v-if="chap.unresolved?.length" class="ssp-section-head">未解决（{{ chap.unresolved.length }}）</div>
            <ul v-if="chap.unresolved?.length" class="ssp-mini-list">
              <li v-for="(u, i) in chap.unresolved" :key="i">{{ u }}</li>
            </ul>
          </li>
        </ul>
      </div>

      <!-- 冲突裁定 -->
      <div v-else-if="activeTab === 'conflicts'">
        <div v-if="conflictEntries.length === 0" class="ssp-empty">暂无角色字段冲突。</div>
        <ul v-else class="ssp-list">
          <li
            v-for="(entry, idx) in conflictEntries"
            :key="idx"
            class="ssp-list-item"
            :class="{ resolved: entry.resolved }"
          >
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ entry.character_name }} · {{ entry.field }}</span>
              <span class="ssp-time">{{ formatTime(entry.at) }}</span>
            </div>
            <div class="ssp-conflict-row">
              <div class="ssp-conflict-cell">
                <div class="ssp-cell-label">既有</div>
                <div class="ssp-cell-body">{{ entry.existing || '(空)' }}</div>
              </div>
              <div class="ssp-conflict-cell">
                <div class="ssp-cell-label">本轮入参</div>
                <div class="ssp-cell-body">{{ entry.incoming || '(空)' }}</div>
              </div>
            </div>
            <div v-if="entry.resolved" class="ssp-resolved-tag">
              已裁定: {{ entry.resolution }}{{ entry.applied ? ' (已写入角色卡)' : '' }}
            </div>
            <div v-else class="ssp-action-row">
              <button class="ssp-btn accept" type="button" :disabled="resolving === idx" @click="resolveConflict(idx, 'accept_incoming')">
                采用本轮
              </button>
              <button class="ssp-btn keep" type="button" :disabled="resolving === idx" @click="resolveConflict(idx, 'keep_existing')">
                保留既有
              </button>
              <button class="ssp-btn dismiss" type="button" :disabled="resolving === idx" @click="resolveConflict(idx, 'dismiss')">
                忽略
              </button>
            </div>
          </li>
        </ul>
      </div>

      <!-- 关系图 -->
      <div v-else-if="activeTab === 'relations'">
        <div v-if="hasRelationshipGraphContent" class="ssp-relation-inspector" data-testid="storydex-relationship-graph">
          <div class="ssp-relation-toolbar" aria-label="relationship graph tools">
            <span class="ssp-relation-mode">关系图谱</span>
            <div class="ssp-relation-controls">
              <button class="ssp-icon-btn" type="button" title="缩小" @click="zoomRelationshipGraph(-0.15)">
                <span class="material-symbols-rounded">zoom_out</span>
              </button>
              <span class="ssp-zoom-label">{{ Math.round(relationshipGraphZoom * 100) }}%</span>
              <button class="ssp-icon-btn" type="button" title="放大" @click="zoomRelationshipGraph(0.15)">
                <span class="material-symbols-rounded">zoom_in</span>
              </button>
              <button class="ssp-icon-btn" type="button" title="重置视图" @click="resetRelationshipGraphView">
                <span class="material-symbols-rounded">center_focus_strong</span>
              </button>
            </div>
          </div>
          <div class="ssp-relation-content">
            <svg
              ref="relationshipGraphSvgRef"
              class="ssp-relation-graph"
              viewBox="0 0 880 520"
              role="img"
              aria-label="Storydex relationship graph"
              @wheel.prevent="handleRelationshipGraphWheel"
              @pointerdown.self="beginRelationshipGraphPan"
              @pointermove="moveRelationshipGraphPointer"
              @pointerup="endRelationshipGraphPointer"
              @pointercancel="endRelationshipGraphPointer"
              @pointerleave="endRelationshipGraphPointer"
            >
              <g class="ssp-relation-viewport" :transform="`translate(${relationshipGraphPan.x} ${relationshipGraphPan.y}) scale(${relationshipGraphZoom})`">
                <path
                  v-for="edge in relationshipGraphEdges"
                  :key="`edge-${edge.index}`"
                  class="ssp-relation-edge"
                  :class="{ selected: !selectedRelationshipNode && selectedRelationshipIndex === edge.index }"
                  :d="edge.pathD"
                  @click.stop="selectRelationshipEdge(edge.index)"
                />
                <g
                  v-for="edge in relationshipGraphEdges"
                  :key="`label-${edge.index}`"
                  class="ssp-relation-edge-label"
                  :class="{ selected: !selectedRelationshipNode && selectedRelationshipIndex === edge.index }"
                  :transform="`translate(${edge.labelX} ${edge.labelY})`"
                  @click.stop="selectRelationshipEdge(edge.index)"
                >
                  <rect :x="-edge.labelWidth / 2" y="-13" :width="edge.labelWidth" height="24" rx="5" />
                  <text text-anchor="middle" dominant-baseline="middle">{{ edge.dimensionLabel }} · {{ edge.edge.current_level }}</text>
                </g>
                <g
                  v-for="node in relationshipGraphNodes"
                  :key="node.id"
                  class="ssp-relation-node"
                  :class="{ active: selectedRelationshipNode?.id === node.id || (!selectedRelationshipNode && selectedRelationshipEdge && (selectedRelationshipEdge.source === node.id || selectedRelationshipEdge.target === node.id)) }"
                  :transform="`translate(${node.x} ${node.y})`"
                  @pointerdown.stop="beginRelationshipNodeDrag($event, node.id)"
                  @click.stop="selectRelationshipNode(node.id)"
                >
                  <circle :r="node.radius" />
                  <text text-anchor="middle" dominant-baseline="middle">{{ node.shortLabel }}</text>
                  <title>{{ node.label }}</title>
                </g>
              </g>
            </svg>
            <aside class="ssp-relation-side-panel">
              <section v-if="selectedRelationshipNode" class="ssp-relation-evidence">
                <div class="ssp-side-title">角色信息</div>
                <div class="ssp-character-name">{{ selectedRelationshipNode.label }}</div>
                <dl class="ssp-side-fields">
                  <template v-for="item in selectedRelationshipNodeInfo" :key="item.label">
                    <dt>{{ item.label }}</dt>
                    <dd>{{ item.value }}</dd>
                  </template>
                </dl>
              </section>
              <section v-else-if="selectedRelationshipEdge" class="ssp-relation-evidence">
                <div class="ssp-side-title">关系信息</div>
                <div class="ssp-list-head">
                  <span class="ssp-tag">{{ selectedRelationshipEdge.source }} → {{ selectedRelationshipEdge.target }}</span>
                </div>
                <div class="ssp-side-kv">
                  <span>关系</span>
                  <strong>{{ relationshipDimensionLabel(selectedRelationshipEdge) }}</strong>
                </div>
                <div class="ssp-side-kv">
                  <span>强度</span>
                  <strong>{{ selectedRelationshipEdge.current_level }}</strong>
                </div>
                <div class="ssp-level-bar">
                  <div
                    class="ssp-level-fill"
                    :style="{ width: levelWidth(selectedRelationshipEdge.current_level), background: levelColor(selectedRelationshipEdge.current_level) }"
                  ></div>
                </div>
                <div v-if="selectedRelationshipLatestHistory" class="ssp-mini-meta">
                  最近 [{{ selectedRelationshipLatestHistory.segment_id }}]
                  {{ selectedRelationshipLatestHistory.delta }}/{{ selectedRelationshipLatestHistory.magnitude }}
                  — {{ selectedRelationshipLatestHistory.detail }}
                </div>
              </section>
            </aside>
          </div>
        </div>
        <div v-if="!hasRelationshipGraphContent" class="ssp-empty">暂无关系记录。</div>
        <div v-else-if="relationshipEdges.length === 0" class="ssp-empty">已有角色，暂无关系边。</div>
        <ul v-else class="ssp-list">
          <li v-for="(edge, idx) in relationshipEdges" :key="idx" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ edge.source }} → {{ edge.target }}</span>
              <span class="ssp-time">{{ edge.dimension }} · level {{ edge.current_level }}</span>
            </div>
            <div class="ssp-level-bar">
              <div class="ssp-level-fill" :style="{ width: levelWidth(edge.current_level), background: levelColor(edge.current_level) }"></div>
            </div>
            <div v-if="edge.history?.length" class="ssp-mini-meta">
              最近: [{{ edge.history[edge.history.length - 1].segment_id }}]
              {{ edge.history[edge.history.length - 1].delta }}/{{ edge.history[edge.history.length - 1].magnitude }}
              — {{ edge.history[edge.history.length - 1].detail }}
            </div>
          </li>
        </ul>
      </div>

      <!-- 伏笔台账 -->
      <div v-else-if="activeTab === 'foreshadow'">
        <div v-if="foreshadowThreads.length === 0" class="ssp-empty">暂无伏笔记录。</div>
        <ul v-else class="ssp-list">
          <li v-for="thread in foreshadowThreads" :key="thread.id" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag" :class="`status-${thread.status}`">{{ thread.id }}</span>
              <span class="ssp-time">{{ thread.status }}{{ thread.id_collisions ? ` · 碰撞 ${thread.id_collisions}` : '' }}</span>
            </div>
            <div v-if="thread.planted_at" class="ssp-mini-meta">
              <strong>plant</strong> [{{ thread.planted_at.segment_id }}]: {{ thread.planted_at.summary }}
            </div>
            <div v-if="thread.callbacks?.length" class="ssp-mini-meta">
              <strong>callbacks</strong>: {{ thread.callbacks.length }} 次（最近 [{{ thread.callbacks[thread.callbacks.length - 1].segment_id }}]）
            </div>
            <div v-if="thread.resolved_at" class="ssp-mini-meta resolved">
              <strong>resolve</strong> [{{ thread.resolved_at.segment_id }}]: {{ thread.resolved_at.summary }}
            </div>
          </li>
        </ul>
      </div>

      <!-- 时间线 -->
      <div v-else-if="activeTab === 'timeline'">
        <div v-if="timelineEntries.length === 0" class="ssp-empty">暂无时间线记录。</div>
        <ul v-else class="ssp-list">
          <li v-for="(entry, idx) in timelineEntries" :key="idx" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ entry.chapter_id || '-' }} · {{ entry.segment_id || '-' }}</span>
              <span class="ssp-time">{{ formatTime(entry.at) }}</span>
            </div>
            <div v-if="entry.advance" class="ssp-snapshot">advance: {{ entry.advance }}</div>
            <div v-if="entry.anchor" class="ssp-mini-meta">anchor: {{ entry.anchor }}</div>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { apiClient, describeTransportError, unwrapEnvelope } from "@/api/client";
import { useStoryStore } from "@/stores/story";
import { useAgentStore } from "@/stores/agent";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import type { ApiEnvelope } from "@/types/api";
import {
  buildWikiEdgeGeometry,
  computeWikiGraphLayout,
  estimateWikiTextWidth,
  placeWikiEdgeLabels,
  placeWikiNodeLabels,
  pointOnWikiEdge,
  wikiEdgeBendOffsets,
  type WikiEdgeGeometry,
  type WikiLabelRect,
  type WikiLayoutBand,
  type WikiLayoutCaption,
} from "@/utils/wikiGraphLayout";

type StoryStateTab = "changes" | "outline" | "conflicts" | "relations" | "foreshadow" | "timeline";

const props = withDefaults(defineProps<{
  initialTab?: StoryStateTab;
  relationshipOnly?: boolean;
  expanded?: boolean;
}>(), {
  initialTab: "changes",
  relationshipOnly: false,
  expanded: false,
});

const storyStore = useStoryStore();
const workspaceStore = useWorkspaceStore();
const agentStore = useAgentStore();
const uiStore = useUiStore();

interface ChangeEntry {
  chapter_id?: string;
  segment_id?: string;
  at?: string;
  written?: string[];
  error_count?: number;
  sizes?: Record<string, number>;
  snapshot_comment?: string;
}

interface OutlineChapter {
  chapter_id: string;
  milestones?: Array<{ segment_id: string; summary: string; evidence?: string; at?: string }>;
  unresolved?: string[];
  arc_summary?: string;
  last_updated_in?: string;
  last_updated_at?: string;
}

interface ConflictEntry {
  character_id: string;
  character_name: string;
  field: string;
  existing: string;
  incoming: string;
  segment_id?: string;
  at?: string;
  resolved?: boolean;
  resolution?: string;
  applied?: boolean;
}

interface RelationshipEdge {
  source: string;
  target: string;
  dimension: string;
  current_level: number;
  history?: Array<{ delta: string; magnitude: string; detail: string; segment_id: string; at: string }>;
  last_updated_in?: string;
  last_updated_at?: string;
}

interface RelationshipGraphNodePayload {
  id?: string;
  label?: string;
  name?: string;
  source?: string;
  kind?: string;
  characterId?: string;
}

interface RelationshipGraphNode {
  id: string;
  label: string;
  x: number;
  y: number;
  radius: number;
  shortLabel: string;
  source?: string;
  kind?: string;
  characterId?: string;
}

interface RelationshipGraphEdge {
  index: number;
  edge: RelationshipEdge;
  source: RelationshipGraphNode;
  target: RelationshipGraphNode;
  pathD: string;
  labelX: number;
  labelY: number;
  labelWidth: number;
  dimensionLabel: string;
}

interface ForeshadowThread {
  id: string;
  status: "open" | "recalled" | "resolved";
  planted_at?: { segment_id: string; at: string; summary: string; evidence?: string };
  callbacks?: Array<{ segment_id: string; at: string; summary: string; evidence?: string; kind?: string }>;
  resolved_at?: { segment_id: string; at: string; summary: string; evidence?: string };
  id_collisions?: number;
}

interface TimelineEntry {
  chapter_id?: string;
  segment_id?: string;
  advance?: string;
  anchor?: string;
  evidence?: string;
  at?: string;
}

interface EvolutionSnapshot {
  currentDir?: string;
  changeLedger?: { entries?: ChangeEntry[] };
  relationshipGraph?: { nodes?: RelationshipGraphNodePayload[]; edges?: RelationshipEdge[] };
  foreshadowLedger?: { threads?: Record<string, ForeshadowThread> };
  chapterOutline?: { chapters?: Record<string, OutlineChapter> };
  characterConflicts?: { entries?: ConflictEntry[] };
  timeline?: { entries?: TimelineEntry[] };
}

interface StoryWikiEntry {
  id: string;
  title: string;
  // 后端仍会下发 overview（项目总览）等不可见分类，这里按原样收下再归一。
  category: string;
  categoryLabel: string;
  summary: string;
  details?: string[];
  sourcePaths?: string[];
  confidence?: number;
  needsReview?: boolean;
  reviewStatus?: string;
  knowledgeStatus?: string;
  sourceRefs?: WikiSourceRef[];
  provenance?: Record<string, unknown>;
  traceId?: string;
  fingerprint?: string;
  updatedAt?: string;
}

interface WikiSourceRef {
  path: string;
  quote: string;
  lineStart?: number;
  lineEnd?: number;
  role?: string;
}

type StoryWikiSyntheticRole = "categoryHub" | "projectHub";
type StoryWikiCategory = "all" | "characters" | "plot" | "setting";

interface StoryWikiNodePayload {
  id: string;
  label: string;
  type: string;
  category?: string;
  entryId?: string;
  summary?: string;
  synthetic?: boolean;
  role?: StoryWikiSyntheticRole;
  selectable?: boolean;
  neighbor?: boolean;
  count?: number;
  needsReviewCount?: number;
  needsReview?: boolean;
  reviewStatus?: string;
  knowledgeStatus?: string;
  narrativeOrder?: number;
}

interface StoryWikiEdgePayload {
  id?: string;
  source: string;
  target: string;
  label: string;
  predicate?: string;
  type: string;
  weight?: number;
  evidence?: string;
  synthetic?: boolean;
  coOccurrence?: boolean;
  level?: number;
  dimension?: string;
  relationType?: string;
  polarity?: string;
  strength?: number;
  status?: string;
  needsReview?: boolean;
  reviewStatus?: string;
  knowledgeStatus?: string;
  confidence?: number | string;
  sourceRefs?: WikiSourceRef[];
  provenance?: Record<string, unknown>;
  traceId?: string;
  fingerprint?: string;
}

interface StoryWikiData {
  projectName: string;
  generatedAt: string;
  generator: string;
  generationMode?: string;
  lastUpdatedAt?: string;
  lastWorkflow?: string;
  lastWorkflowStatus?: string;
  schemaVersion?: number;
  knowledgeRevision?: number;
  builtFromRevision?: number;
  lastSuccessfulRevision?: number;
  status?: string;
  diagnostics?: Array<Record<string, unknown>>;
  changedSourcePaths?: string[];
  agent?: {
    attempted?: boolean;
    completed?: boolean;
    traceId?: string;
    errorMessage?: string;
    eventCount?: number;
  };
  summary: string;
  categoryLabels?: Record<string, string>;
  nodeTypeLabels?: Record<string, string>;
  entries: StoryWikiEntry[];
  graph: {
    nodes: StoryWikiNodePayload[];
    edges: StoryWikiEdgePayload[];
  };
}

interface StoryWikiGraphQueryResponse {
  mode: "category" | "search" | "entry" | "node" | "overview" | "unavailable";
  queryStatus?: string;
  query: string;
  category: string;
  entryId: string;
  nodeId: string;
  depth: number;
  limit: number;
  schemaVersion?: number;
  knowledgeRevision?: number;
  builtFromRevision?: number;
  lastSuccessfulRevision?: number;
  status?: string;
  entries: StoryWikiEntry[];
  graph: {
    nodes: StoryWikiNodePayload[];
    edges: StoryWikiEdgePayload[];
  };
  matchedEntryIds: string[];
  total: {
    entryCount: number;
    nodeCount: number;
    edgeCount: number;
    confirmedEdgeCount?: number;
    reviewRequiredEdgeCount?: number;
    connectedNodeCount?: number;
    isolatedNodeCount?: number;
  };
  offset?: number;
  includeReview?: boolean;
  returnedNodeCount?: number;
  hasMore?: boolean;
  nextOffset?: number | null;
  pagination?: {
    offset: number;
    limit: number;
    returnedNodeCount: number;
    hasMore: boolean;
    nextOffset?: number | null;
  };
}

interface WikiReviewCandidate {
  id: string;
  subjectId?: string;
  subject?: string;
  predicate?: string;
  objectId?: string;
  object?: string;
  reviewStatus?: string;
  knowledgeStatus?: string;
  confidence?: number | string;
  sourceRefs?: WikiSourceRef[];
  provenance?: Record<string, unknown>;
  traceId?: string;
  fingerprint?: string;
  targetSourcePath?: string;
  usesSidecar?: boolean;
  reviewReason?: string;
  evidenceHash?: string;
  createdAt?: string;
  updatedAt?: string;
}

interface WikiReviewDraft {
  subjectId: string;
  predicate: string;
  objectId: string;
  targetSourcePath: string;
  rejectReason: string;
  rejectNote: string;
}

interface WikiGraphNode {
  id: string;
  label: string;
  type: string;
  category: string;
  entryId: string;
  summary: string;
  synthetic: boolean;
  role: StoryWikiSyntheticRole | "";
  selectable: boolean;
  neighbor: boolean;
  degree: number;
  tone: string;
  count: number;
  needsReviewCount: number;
  needsReview: boolean;
  x: number;
  y: number;
  radius: number;
  shortLabel: string;
  glyph: string;
  band: WikiLayoutBand;
  narrativeOrder: number;
  labelWidth: number;
}

interface WikiGraphEdge {
  id: string;
  label: string;
  displayLabel: string;
  semanticsLabel: string;
  type: string;
  evidence: string;
  synthetic: boolean;
  active: boolean;
  coOccurrence: boolean;
  realRelation: boolean;
  level: number | null;
  dimension: string;
  source: WikiGraphNode;
  target: WikiGraphNode;
  pathD: string;
  labelX: number;
  labelY: number;
  labelWidth: number;
  reviewStatus: string;
  knowledgeStatus: string;
  confidence: number | string | null;
  sourceRefs: WikiSourceRef[];
  provenance: Record<string, unknown>;
  traceId: string;
  fingerprint: string;
  arrowD: string;
  geometry: WikiEdgeGeometry;
  structural: boolean;
  strokeWidth: number;
}

const loading = ref(false);
const errorMessage = ref("");
const snapshot = ref<EvolutionSnapshot>({});
const wikiLoading = ref(false);
const wikiRebuilding = ref(false);
const wikiAgentStatus = ref("");
const wikiAgentTone = ref<"idle" | "success" | "warning" | "error">("idle");
const wikiErrorMessage = ref("");
const wikiData = ref<StoryWikiData | null>(null);
const wikiGraphQueryData = ref<StoryWikiGraphQueryResponse | null>(null);
const wikiGraphLoading = ref(false);
const wikiGraphHasMore = ref(false);
const wikiGraphNextOffset = ref<number | null>(null);
const wikiGraphSearchInput = ref("");
const wikiGraphSearchQuery = ref("");
const wikiKnowledgeStatusFilter = ref("all");
const wikiSyncErrorMessage = ref("");
const wikiReviewQueue = ref<WikiReviewCandidate[]>([]);
const wikiReviewTotal = ref(0);
const wikiReviewLoading = ref(false);
const wikiReviewError = ref("");
const wikiReviewOpen = ref(false);
const wikiReviewActionId = ref("");
const wikiReviewDrafts = ref<Record<string, WikiReviewDraft>>({});
const selectedWikiCategory = ref<StoryWikiCategory>("all");
const selectedWikiEntryId = ref("");
const selectedWikiNodeId = ref("");
const selectedWikiEdgeId = ref("");
const hoveredWikiNodeId = ref("");
const hoveredWikiEdgeId = ref("");
const wikiGraphZoom = ref(1);
const wikiGraphPan = ref({ x: 0, y: 0 });
const wikiGraphSvgRef = ref<SVGSVGElement | null>(null);
const wikiGraphPanelRef = ref<HTMLElement | null>(null);
const wikiInspectorRef = ref<HTMLElement | null>(null);
const wikiCanvasSize = ref({ width: 960, height: 600 });
const wikiLayoutPositions = ref<Record<string, { x: number; y: number }>>({});
const wikiLayoutBands = ref<Record<string, WikiLayoutBand>>({});
const wikiLayoutCaptions = ref<WikiLayoutCaption[]>([]);
// 窄宽度下详情栏浮在图谱右侧，由选中/开关控制；宽度足够时详情栏常驻，忽略此状态。
const wikiInspectorOpen = ref(false);
// 详情栏只展示用户明确选中的条目或节点；未选中时显示项目概览，图上也不高亮任何节点。
const wikiDetailPinned = ref(false);
let wikiGraphPanMoved = false;
const wikiNodeOverrides = ref<Record<string, { x: number; y: number }>>({});
const wikiGraphDragState = ref<{ nodeId: string; pointerId: number; offsetX: number; offsetY: number } | null>(null);
const wikiGraphPanState = ref<{
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
} | null>(null);
const activeTab = ref<StoryStateTab>(props.relationshipOnly ? "relations" : props.initialTab);
const resolving = ref<number | null>(null);
const rollingBack = ref<number | null>(null);
const lastRollbackId = ref("");
const relationshipGraphSvgRef = ref<SVGSVGElement | null>(null);
const relationshipGraphZoom = ref(1);
const relationshipGraphPan = ref({ x: 0, y: 0 });
const selectedRelationshipIndex = ref(0);
const selectedRelationshipNodeId = ref("");
const relationshipNodeOverrides = ref<Record<string, { x: number; y: number }>>({});
const relationshipDragState = ref<{
  nodeId: string;
  pointerId: number;
  offsetX: number;
  offsetY: number;
} | null>(null);
const relationshipPanState = ref<{
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
} | null>(null);
let snapshotLoadSeq = 0;
let wikiGraphLoadSeq = 0;

const RELATION_GRAPH_WIDTH = 880;
const RELATION_GRAPH_HEIGHT = 520;
const RELATION_GRAPH_CENTER_X = RELATION_GRAPH_WIDTH / 2;
const RELATION_GRAPH_CENTER_Y = RELATION_GRAPH_HEIGHT / 2;
const WIKI_CATEGORY_TABS: Array<{ id: StoryWikiCategory; label: string; icon: string }> = [
  { id: "all", label: "全部", icon: "hub" },
  { id: "characters", label: "角色", icon: "groups" },
  { id: "plot", label: "剧情", icon: "timeline" },
  { id: "setting", label: "设定", icon: "auto_stories" },
];
// 节点色组：type -> 图例色调。granular type 折叠成四个视觉组，避免五颜六色。
const WIKI_NODE_TONES: Record<string, string> = {
  character: "character",
  chapter: "plot",
  event: "plot",
  timeline: "plot",
  world: "setting",
  setting: "setting",
  item: "setting",
  location: "setting",
  faction: "setting",
  foreshadow: "setting",
  project: "hub",
  categoryHub: "hub",
};
const WIKI_TONE_LABELS: Record<string, string> = {
  character: "角色",
  plot: "剧情",
  setting: "设定",
  hub: "导航",
  misc: "其他",
};
// 节点内的类型图标（Material Symbols 连字），与分类色一起区分条目类型。
const WIKI_NODE_GLYPHS: Record<string, string> = {
  character: "person",
  chapter: "menu_book",
  event: "flag",
  timeline: "schedule",
  world: "public",
  setting: "public",
  item: "diamond",
  location: "location_on",
  faction: "shield",
  foreshadow: "visibility",
  document: "description",
  project: "hub",
  categoryHub: "hub",
};
const WIKI_CATEGORY_TONES: Record<string, string> = {
  characters: "character",
  plot: "plot",
  setting: "setting",
};
const LOCAL_WIKI_GENERATORS = new Set(["local-fallback-wiki", "local-evidence-grounded-wiki", "storydex-rust-knowledge"]);

const panelLoading = computed(() => (
  props.relationshipOnly ? wikiLoading.value || wikiRebuilding.value : loading.value
));

const changeEntries = computed<ChangeEntry[]>(() => {
  const entries = snapshot.value.changeLedger?.entries ?? [];
  return [...entries].reverse().slice(0, 30);
});

const outlineChapters = computed<OutlineChapter[]>(() => {
  const chapters = snapshot.value.chapterOutline?.chapters ?? {};
  return Object.values(chapters);
});

const conflictEntries = computed<ConflictEntry[]>(() => {
  return snapshot.value.characterConflicts?.entries ?? [];
});

const relationshipEdges = computed<RelationshipEdge[]>(() => {
  return snapshot.value.relationshipGraph?.edges ?? [];
});

const relationshipGraphNodes = computed<RelationshipGraphNode[]>(() => {
  const ids = new Set<string>();
  const payloadById = new Map<string, RelationshipGraphNodePayload>();
  (snapshot.value.relationshipGraph?.nodes ?? []).forEach((node) => {
    const nodeId = relationshipNodePayloadId(node);
    if (!nodeId) {
      return;
    }
    ids.add(nodeId);
    payloadById.set(nodeId, node);
  });
  relationshipEdges.value.forEach((edge) => {
    if (edge.source) ids.add(edge.source);
    if (edge.target) ids.add(edge.target);
  });
  const names = Array.from(ids);
  const count = Math.max(1, names.length);
  const radiusX = count <= 2 ? 260 : 315;
  const radiusY = count <= 2 ? 0 : 180;
  return names.map((id, index) => {
    const angle = count === 1 ? -Math.PI / 2 : (Math.PI * 2 * index) / count - Math.PI / 2;
    const fallback = {
      x: RELATION_GRAPH_CENTER_X + Math.cos(angle) * radiusX,
      y: RELATION_GRAPH_CENTER_Y + Math.sin(angle) * radiusY,
    };
    if (count === 2) {
      fallback.y = RELATION_GRAPH_CENTER_Y;
    }
    const payload = payloadById.get(id);
    const label = relationshipNodePayloadLabel(payload, id);
    const override = relationshipNodeOverrides.value[id];
    return {
      id,
      label,
      x: override?.x ?? fallback.x,
      y: override?.y ?? fallback.y,
      radius: Math.max(34, Math.min(54, 28 + label.length * 2)),
      shortLabel: shortNodeLabel(label),
      source: payload?.source,
      kind: payload?.kind,
      characterId: payload?.characterId,
    };
  });
});

const relationshipGraphEdges = computed<RelationshipGraphEdge[]>(() => {
  const nodeById = new Map(relationshipGraphNodes.value.map((node) => [node.id, node]));
  return relationshipEdges.value.flatMap((edge, index) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) {
      return [];
    }
    const dimensionLabel = relationshipDimensionLabel(edge);
    return [
      {
        index,
        edge,
        source,
        target,
        pathD: buildRelationshipEdgePath(source, target, index),
        labelX: buildRelationshipEdgeLabelPoint(source, target, index).x,
        labelY: buildRelationshipEdgeLabelPoint(source, target, index).y - 14,
        labelWidth: Math.max(76, Math.min(150, `${dimensionLabel} ${edge.current_level}`.length * 12 + 34)),
        dimensionLabel,
      },
    ];
  });
});

const hasRelationshipGraphContent = computed(() => relationshipGraphNodes.value.length > 0 || relationshipEdges.value.length > 0);

const selectedRelationshipEdge = computed<RelationshipEdge | null>(() => {
  if (relationshipEdges.value.length === 0) {
    return null;
  }
  return relationshipEdges.value[selectedRelationshipIndex.value] ?? relationshipEdges.value[0] ?? null;
});

const selectedRelationshipNode = computed<RelationshipGraphNode | null>(() => {
  if (!selectedRelationshipNodeId.value) {
    return null;
  }
  return relationshipGraphNodes.value.find((node) => node.id === selectedRelationshipNodeId.value) ?? null;
});

const selectedRelationshipNodeInfo = computed<Array<{ label: string; value: string }>>(() => {
  const node = selectedRelationshipNode.value;
  if (!node) {
    return [];
  }
  return [
    { label: "节点 ID", value: node.id },
    { label: "类型", value: formatRelationshipNodeKind(node.kind) },
    { label: "来源", value: node.source || "关系图快照" },
    ...(node.characterId && node.characterId !== node.id ? [{ label: "角色 ID", value: node.characterId }] : []),
  ];
});

const selectedRelationshipLatestHistory = computed(() => {
  const history = selectedRelationshipEdge.value?.history ?? [];
  return history[history.length - 1] ?? null;
});

const foreshadowThreads = computed<ForeshadowThread[]>(() => {
  const threads = snapshot.value.foreshadowLedger?.threads ?? {};
  return Object.values(threads);
});

const timelineEntries = computed<TimelineEntry[]>(() => {
  const entries = snapshot.value.timeline?.entries ?? [];
  return [...entries].reverse().slice(0, 30);
});

const tabs = computed(() => [
  { id: "changes" as const, label: "变更", count: changeEntries.value.length },
  { id: "outline" as const, label: "大纲", count: outlineChapters.value.length },
  { id: "conflicts" as const, label: "冲突", count: conflictEntries.value.filter((e) => !e.resolved).length },
  { id: "relations" as const, label: "关系", count: relationshipGraphNodes.value.length || relationshipEdges.value.length },
  { id: "foreshadow" as const, label: "伏笔", count: foreshadowThreads.value.length },
  { id: "timeline" as const, label: "时间", count: timelineEntries.value.length },
]);

const visibleTabs = computed(() => (
  props.relationshipOnly ? tabs.value.filter((tab) => tab.id === "relations") : tabs.value
));

const wikiEntries = computed<StoryWikiEntry[]>(() => wikiData.value?.entries ?? []);

const wikiCategoryTabs = computed(() => {
  // 计数按条目自身分类，overview 这类不可见分类不该被兜底算进角色 tab。
  const counts = new Map<string, number>();
  wikiEntries.value.forEach((entry) => {
    const category = String(entry.category ?? "").trim();
    counts.set(category, (counts.get(category) ?? 0) + 1);
  });
  const labels = wikiData.value?.categoryLabels ?? {};
  return WIKI_CATEGORY_TABS.map((tab) => ({
    ...tab,
    label: labels[tab.id] || tab.label,
    count: tab.id === "all" ? wikiEntries.value.filter((entry) => entry.category !== "overview").length : counts.get(tab.id) ?? 0,
  }));
});

const selectedWikiCategoryEntries = computed(() => {
  const category = selectedWikiCategory.value;
  return wikiEntries.value.filter((entry) => category === "all" ? entry.category !== "overview" : entry.category === category);
});

function matchesWikiKnowledgeStatus(status: unknown): boolean {
  const filter = wikiKnowledgeStatusFilter.value;
  return filter === "all" || String(status || "") === filter;
}

const visibleWikiEntries = computed(() => {
  const entries = wikiGraphQueryData.value?.entries ?? selectedWikiCategoryEntries.value;
  return entries.filter((entry) => matchesWikiKnowledgeStatus(entry.knowledgeStatus));
});

const wikiProjectionStatus = computed(() => (
  wikiGraphQueryData.value?.status || wikiData.value?.status || "ready"
));

const wikiRevisionLabel = computed(() => {
  const knowledgeRevision = wikiGraphQueryData.value?.knowledgeRevision ?? wikiData.value?.knowledgeRevision;
  const builtFromRevision = wikiGraphQueryData.value?.builtFromRevision ?? wikiData.value?.builtFromRevision;
  if (typeof knowledgeRevision !== "number" || typeof builtFromRevision !== "number") {
    return "";
  }
  return `版本 ${knowledgeRevision}/${builtFromRevision}`;
});

const selectedWikiEntry = computed<StoryWikiEntry | null>(() => {
  if (!wikiEntries.value.length) {
    return null;
  }
  return (
    wikiEntries.value.find((entry) => entry.id === selectedWikiEntryId.value) ??
    visibleWikiEntries.value[0] ??
    wikiEntries.value[0] ??
    null
  );
});

const selectedWikiCategoryLabel = computed(() => (
  wikiCategoryTabs.value.find((category) => category.id === selectedWikiCategory.value)?.label || "角色"
));

const selectedWikiNode = computed<WikiGraphNode | null>(() => {
  if (!selectedWikiNodeId.value) {
    return null;
  }
  return wikiGraphNodes.value.find((node) => node.id === selectedWikiNodeId.value) ?? null;
});

const selectedWikiRelationEdge = computed<WikiGraphEdge | null>(() => {
  if (!selectedWikiEdgeId.value) {
    return null;
  }
  return wikiGraphEdges.value.find((edge) => edge.id === selectedWikiEdgeId.value) ?? null;
});

// 节点连接度：驱动节点大小与布局向心力，主干角色一眼可辨。
const wikiGraphDegrees = computed<Map<string, number>>(() => {
  const degrees = new Map<string, number>();
  const visibleNodeIds = new Set(
    (wikiGraphQueryData.value?.graph?.nodes ?? [])
      .filter((node) => matchesWikiKnowledgeStatus(node.knowledgeStatus))
      .map((node) => node.id),
  );
  (wikiGraphQueryData.value?.graph?.edges ?? []).forEach((edge) => {
    if (!visibleNodeIds.has(edge.source) || !visibleNodeIds.has(edge.target)) {
      return;
    }
    degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1);
    degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1);
  });
  return degrees;
});

const wikiIsolatedRawGraphNodes = computed<StoryWikiNodePayload[]>(() => {
  const rawNodes = wikiGraphQueryData.value?.graph?.nodes ?? [];
  const degrees = wikiGraphDegrees.value;
  return rawNodes.filter((node) => (
    matchesWikiKnowledgeStatus(node.knowledgeStatus)
    && !node.synthetic
    && (degrees.get(node.id) ?? 0) === 0
  ));
});

const wikiVisibleRawGraphNodes = computed<StoryWikiNodePayload[]>(() => {
  return (wikiGraphQueryData.value?.graph?.nodes ?? [])
    .filter((node) => matchesWikiKnowledgeStatus(node.knowledgeStatus));
});

// 节点大小按连接度的平方根增长：主干角色明显更大，又不会把周围挤散。
function wikiNodeRadius(node: StoryWikiNodePayload, degree: number): number {
  if (node.synthetic) {
    return node.role === "projectHub" ? 22 : 18;
  }
  if (node.neighbor) {
    return 11 + Math.min(4, Math.sqrt(Math.max(0, degree)) * 1.4);
  }
  return 13 + Math.min(9, Math.sqrt(Math.max(0, degree)) * 3);
}

function wikiNodeLabel(value: string, maxLength = 15): string {
  const trimmed = String(value || "").trim();
  return trimmed.length <= maxLength ? trimmed : `${trimmed.slice(0, maxLength - 1)}…`;
}

// 条目摘要来自 Markdown 正文的压平文本：去掉开头的标题符号与重复的条目名，列表符号改为间隔号。
function cleanWikiSummary(value: unknown, title = ""): string {
  let text = String(value ?? "").replace(/\s+/g, " ").trim();
  text = text.replace(/^#{1,6}\s+/, "");
  const name = String(title || "").trim();
  if (name && text.startsWith(name) && text.length > name.length) {
    text = text.slice(name.length).replace(/^[\s:：·、,，.-]+/, "");
  }
  return text
    .replace(/\s#{1,6}\s+/g, " ")
    .replace(/\s+[-*•]\s+/g, " · ")
    .replace(/^[-*•]\s+/, "")
    .trim();
}

function wikiEntryTone(entry: Pick<StoryWikiEntry, "category">): string {
  return WIKI_CATEGORY_TONES[String(entry.category || "")] ?? "misc";
}

const wikiLabelScale = computed(() => clampNumber(Number(uiStore.centerPaneFontScale || 100) / 100, 0.75, 1.5));

function resolveWikiDisplayLabel(value: unknown): string {
  const label = normalizeRelationshipGraphText(value);
  if (!label || /^(?:source|entity|character|char|event|mention|rel|unresolved):/i.test(label)) {
    return "未解析实体";
  }
  return label;
}

function wikiReviewStatusLabel(status?: string): string {
  return ({ confirmed: "已确认", rejected: "已驳回", review_required: "待确认" } as Record<string, string>)[status || "review_required"] || "待确认";
}

function wikiEndpointLabel(id?: string, label?: string): string {
  const node = wikiGraphQueryData.value?.graph?.nodes?.find((node) => node.id === id);
  return resolveWikiDisplayLabel(node?.label || label || "缺失条目");
}

function wikiEndpointOptions(candidate: WikiReviewCandidate): { id: string; label: string }[] {
  const options = new Map((wikiGraphQueryData.value?.graph?.nodes || [])
    .filter((node) => node.type !== "project" && !node.synthetic)
    .map((node) => [node.id, { id: node.id, label: resolveWikiDisplayLabel(node.label || "缺失条目") }]));
  for (const [id, label] of [[candidate.subjectId, candidate.subject], [candidate.objectId, candidate.object]]) {
    if (id && !options.has(id)) options.set(id, { id, label: wikiEndpointLabel(id, label) });
  }
  return [...options.values()];
}

const WIKI_RELATION_TYPE_LABELS: Record<string, string> = {
  professional_collaboration: "专业合作",
  family: "家族",
  trust: "信任",
  intimacy: "亲密",
  hostility: "敌对",
  loyalty: "忠诚",
  alliance: "同盟",
  rivalry: "竞争",
};

const WIKI_RELATION_POLARITY_LABELS: Record<string, string> = {
  positive: "正向",
  neutral: "中立",
  negative: "负向",
};

function wikiRelationshipSemanticsLabel(edge: Partial<StoryWikiEdgePayload>): string {
  const relationType = normalizeRelationshipGraphText(edge.relationType).toLowerCase();
  const dimension = normalizeRelationshipGraphText(edge.dimension).toLowerCase();
  const label = WIKI_RELATION_TYPE_LABELS[relationType]
    || RELATIONSHIP_DIMENSION_LABELS[dimension]
    || normalizeRelationshipGraphText(edge.label)
    || "关系";
  const parts = [label];
  const polarity = normalizeRelationshipGraphText(edge.polarity).toLowerCase();
  if (WIKI_RELATION_POLARITY_LABELS[polarity]) {
    parts.push(WIKI_RELATION_POLARITY_LABELS[polarity]);
  }
  if (typeof edge.strength === "number" && Number.isFinite(edge.strength)) {
    const strength = Math.abs(edge.strength);
    parts.push(strength >= 0.75 ? "高强度" : strength >= 0.4 ? "中等强度" : "低强度");
  }
  return parts.join(" · ");
}

function wikiNodeTone(node: Pick<StoryWikiNodePayload, "type" | "synthetic">): string {
  const type = normalizeRelationshipGraphText(node.type || "project").replace(/\s+/g, "-");
  return WIKI_NODE_TONES[type] ?? (node.synthetic ? "hub" : "misc");
}

function wikiNodeNarrativeOrder(node: StoryWikiNodePayload): number {
  // 只有章节进入叙事主线；事件等条目即使带序号也留在关系网里。
  return node.type === "chapter" && typeof node.narrativeOrder === "number" && node.narrativeOrder > 0
    ? node.narrativeOrder
    : 0;
}

let wikiLayoutSignature = "";
function recomputeWikiLayout(force = false): void {
  const rawNodes = wikiVisibleRawGraphNodes.value;
  if (!rawNodes.length) {
    wikiLayoutPositions.value = {};
    wikiLayoutBands.value = {};
    wikiLayoutCaptions.value = [];
    wikiLayoutSignature = "";
    return;
  }
  const degrees = wikiGraphDegrees.value;
  const previous = wikiLayoutPositions.value;
  const layoutNodes = rawNodes.map((node) => ({
    id: node.id,
    radius: wikiNodeRadius(node, degrees.get(node.id) ?? 0),
    tone: wikiNodeTone(node),
    narrativeOrder: wikiNodeNarrativeOrder(node),
    seedX: previous[node.id]?.x,
    seedY: previous[node.id]?.y,
  }));
  const layoutEdges = (wikiGraphQueryData.value?.graph?.edges ?? []).map((edge) => ({
    source: edge.source,
    target: edge.target,
    weight: typeof edge.weight === "number" ? edge.weight : 1,
  }));
  // 签名只含影响布局的字段：标签文字与证据变化不重排节点。
  const signature = JSON.stringify([
    wikiCanvasSize.value,
    layoutNodes.map(({ id, radius, tone, narrativeOrder }) => [id, radius, tone, narrativeOrder]).sort(),
    layoutEdges.map(({ source, target, weight }) => [source, target, weight]).sort(),
  ]);
  if (!force && signature === wikiLayoutSignature) return;
  wikiLayoutSignature = signature;
  const layout = computeWikiGraphLayout(layoutNodes, layoutEdges, {
    width: wikiCanvasSize.value.width,
    height: wikiCanvasSize.value.height,
  });
  wikiLayoutPositions.value = layout.positions;
  wikiLayoutBands.value = layout.bands;
  wikiLayoutCaptions.value = layout.captions;
}

const wikiGraphNodes = computed<WikiGraphNode[]>(() => {
  const rawNodes = wikiVisibleRawGraphNodes.value;
  const layout = wikiLayoutPositions.value;
  const overrides = wikiNodeOverrides.value;
  const degrees = wikiGraphDegrees.value;
  const centerX = wikiCanvasSize.value.width / 2;
  const centerY = wikiCanvasSize.value.height / 2;
  const spread = Math.max(80, Math.min(centerX, centerY) * 0.62);
  const bands = wikiLayoutBands.value;
  const labelScale = wikiLabelScale.value;
  return rawNodes.map((node, index) => {
    const label = resolveWikiDisplayLabel(node.label || node.id);
    const degree = degrees.get(node.id) ?? 0;
    const angle = (Math.PI * 2 * index) / Math.max(1, rawNodes.length) - Math.PI / 2;
    const fallback = {
      x: centerX + Math.cos(angle) * spread,
      y: centerY + Math.sin(angle) * spread,
    };
    const position = overrides[node.id] ?? layout[node.id] ?? fallback;
    const type = normalizeRelationshipGraphText(node.type || "project").replace(/\s+/g, "-");
    const band = bands[node.id] ?? "network";
    const shortLabel = wikiNodeLabel(label, band === "shelf" ? 8 : band === "spine" ? 11 : 14);
    const labelFontSize = (band === "shelf" || node.neighbor ? 11 : 12) * labelScale;
    return {
      id: node.id,
      label,
      type,
      category: node.category || "",
      entryId: node.entryId || "",
      summary: node.summary || "",
      synthetic: Boolean(node.synthetic),
      role: node.role || "",
      selectable: node.selectable !== false && !node.synthetic,
      neighbor: Boolean(node.neighbor),
      degree,
      tone: WIKI_NODE_TONES[type] ?? (node.synthetic ? "hub" : "misc"),
      count: typeof node.count === "number" ? node.count : 0,
      needsReviewCount: typeof node.needsReviewCount === "number" ? node.needsReviewCount : 0,
      needsReview: Boolean(node.needsReview),
      x: position.x,
      y: position.y,
      radius: wikiNodeRadius(node, degree),
      shortLabel,
      glyph: WIKI_NODE_GLYPHS[type] ?? "label",
      band,
      narrativeOrder: wikiNodeNarrativeOrder(node),
      labelWidth: estimateWikiTextWidth(shortLabel, labelFontSize) + 4,
    };
  });
});

// 节点名位置：默认在圆点下方，与其他节点或标签重叠时自动换到上/右/左。
const wikiNodeLabelLayout = computed(() => placeWikiNodeLabels(wikiGraphNodes.value.map((node) => ({
  id: node.id,
  x: node.x,
  y: node.y,
  radius: node.radius,
  width: node.labelWidth,
  height: 15 * wikiLabelScale.value,
  priority: (node.band === "network" ? 100 : node.band === "spine" ? 50 : 0) + node.degree * 4 + node.radius,
}))));

const wikiGraphEdges = computed<WikiGraphEdge[]>(() => {
  const nodeById = new Map(wikiGraphNodes.value.map((node) => [node.id, node]));
  const rawEdges = (wikiGraphQueryData.value?.graph?.edges ?? [])
    .filter((edge) => nodeById.has(edge.source) && nodeById.has(edge.target));
  // 同一对节点的多条边（多维度关系）对称弯开，单条边保持直线。
  const bends = wikiEdgeBendOffsets(rawEdges);
  const labelScale = wikiLabelScale.value;
  return rawEdges.map((edge, index) => {
    const source = nodeById.get(edge.source)!;
    const target = nodeById.get(edge.target)!;
    const label = normalizeRelationshipGraphText(edge.label || edge.type || "关联");
    const semanticsLabel = edge.type === "relationship"
      ? wikiRelationshipSemanticsLabel(edge)
      : label;
    const level = typeof edge.level === "number" ? edge.level : null;
    const displayLabel = edge.type === "relationship"
      ? semanticsLabel.split(" · ")[0]
      : label;
    const type = normalizeRelationshipGraphText(edge.type || "related").replace(/\s+/g, "-");
    const structural = type === "timeline";
    const directed = structural || type === "relationship";
    const geometry = buildWikiEdgeGeometry(source, target, bends[index] ?? 0, {
      gap: 3,
      arrow: directed ? 7 : 0,
    });
    const midpoint = pointOnWikiEdge(geometry, 0.5);
    const id = String(edge.id || edge.fingerprint || `${edge.source}-${edge.target}-${index}`);
    const coOccurrence = Boolean(edge.coOccurrence);
    const strength = typeof edge.strength === "number" && Number.isFinite(edge.strength) ? Math.abs(edge.strength) : null;
    return {
      id,
      label,
      displayLabel,
      semanticsLabel,
      type,
      evidence: normalizeRelationshipGraphText(edge.evidence || ""),
      synthetic: Boolean(edge.synthetic),
      active: isWikiEdgeActive({ id, source, target }),
      coOccurrence,
      realRelation: edge.type === "relationship" && !coOccurrence,
      level,
      dimension: edge.dimension || "",
      reviewStatus: String(edge.reviewStatus || (edge.needsReview ? "review_required" : "confirmed")),
      knowledgeStatus: String(edge.knowledgeStatus || "observed"),
      confidence: edge.confidence ?? null,
      sourceRefs: Array.isArray(edge.sourceRefs) ? edge.sourceRefs : [],
      provenance: edge.provenance && typeof edge.provenance === "object" ? edge.provenance : {},
      traceId: String(edge.traceId || ""),
      fingerprint: String(edge.fingerprint || ""),
      source,
      target,
      pathD: geometry.pathD,
      arrowD: geometry.arrowD,
      geometry,
      structural,
      strokeWidth: structural ? 1.4 : strength === null ? 1.5 : 1.2 + strength * 1.4,
      labelX: midpoint.x,
      labelY: midpoint.y,
      labelWidth: Math.ceil(estimateWikiTextWidth(displayLabel, 11 * labelScale) + 18),
    };
  });
});

// 焦点态：hover/选中节点时只保留其邻域，其余淡出，密图也能一眼看清。
const wikiGraphFocusCenterId = computed(() => hoveredWikiNodeId.value || selectedWikiNodeId.value);

const wikiGraphFocusIds = computed<Set<string> | null>(() => {
  const centerId = wikiGraphFocusCenterId.value;
  if (centerId) {
    const ids = new Set<string>([centerId]);
    (wikiGraphQueryData.value?.graph?.edges ?? []).forEach((edge) => {
      if (edge.source === centerId) {
        ids.add(edge.target);
      }
      if (edge.target === centerId) {
        ids.add(edge.source);
      }
    });
    return ids;
  }
  if (selectedWikiEdgeId.value) {
    const edge = wikiGraphEdges.value.find((item) => item.id === selectedWikiEdgeId.value);
    if (edge) {
      return new Set([edge.source.id, edge.target.id]);
    }
  }
  return null;
});

function isWikiNodeDimmed(node: WikiGraphNode): boolean {
  const focus = wikiGraphFocusIds.value;
  return focus !== null && !focus.has(node.id);
}

function isWikiEdgeDimmed(edge: WikiGraphEdge): boolean {
  const focus = wikiGraphFocusIds.value;
  if (focus === null) {
    return false;
  }
  const centerId = wikiGraphFocusCenterId.value;
  if (centerId) {
    return edge.source.id !== centerId && edge.target.id !== centerId;
  }
  return !(focus.has(edge.source.id) && focus.has(edge.target.id));
}

// 边标签默认克制：真实关系边常显（关系名即核心信息）；章节承接等结构边只在 hover/选中时出现。
// 标签沿连线滑动避让节点、节点名与其他标签；常显标签先放，hover 临时出现的标签后放，避免已有标签跳动。
const visibleWikiGraphLabelEdges = computed(() => {
  const hoveredId = hoveredWikiEdgeId.value;
  const selectedEdgeId = selectedWikiEdgeId.value;
  const stable: WikiGraphEdge[] = [];
  const transient: WikiGraphEdge[] = [];
  wikiGraphEdges.value.forEach((edge) => {
    if (!edge.pathD) {
      return;
    }
    if (!edge.structural && (edge.active || (edge.realRelation && !isWikiEdgeDimmed(edge)))) {
      stable.push(edge);
    } else if (hoveredId === edge.id || selectedEdgeId === edge.id) {
      transient.push(edge);
    }
  });
  const ordered = [...stable, ...transient];
  if (!ordered.length) {
    return [];
  }
  const labelLayout = wikiNodeLabelLayout.value;
  const labelHeight = 18 * wikiLabelScale.value;
  const obstacles: WikiLabelRect[] = [];
  wikiGraphNodes.value.forEach((node) => {
    obstacles.push({ x: node.x - node.radius - 2, y: node.y - node.radius - 2, width: node.radius * 2 + 4, height: node.radius * 2 + 4 });
    const rect = labelLayout[node.id]?.rect;
    if (rect) obstacles.push(rect);
  });
  const placements = placeWikiEdgeLabels(ordered.map((edge) => ({
    id: edge.id,
    width: edge.labelWidth,
    height: labelHeight,
    candidates: [0.5, 0.36, 0.64, 0.26, 0.74].map((t) => pointOnWikiEdge(edge.geometry, t)),
  })), obstacles);
  return ordered.map((edge) => ({
    ...edge,
    labelX: placements[edge.id]?.x ?? edge.labelX,
    labelY: placements[edge.id]?.y ?? edge.labelY,
  }));
});

const wikiGraphLegend = computed(() => {
  const counts = new Map<string, number>();
  wikiGraphNodes.value.forEach((node) => {
    if (!node.synthetic) counts.set(node.tone, (counts.get(node.tone) ?? 0) + 1);
  });
  // 三视图对应三色，misc 兜底未归类节点；hub 节点已经不再产出。
  return ["character", "plot", "setting", "misc"]
    .filter((key) => counts.has(key))
    .map((key) => ({ key, label: WIKI_TONE_LABELS[key] ?? key, count: counts.get(key) ?? 0 }));
});

// 连线图例：说明线型并给出数量；关系数优先取后端统计（覆盖未加载的分页）。
const wikiEdgeLegend = computed(() => {
  const edges = wikiGraphEdges.value;
  const stats = wikiGraphStats.value;
  const loadedRelations = edges.filter((edge) => edge.realRelation && edge.reviewStatus !== "review_required").length;
  const loadedReview = edges.filter((edge) => edge.reviewStatus === "review_required").length;
  const items: Array<{ key: string; label: string; count: number | null }> = [];
  const relationCount = stats.confirmedEdgeCount || loadedRelations;
  if (relationCount > 0) items.push({ key: "relation", label: "关系", count: relationCount });
  const reviewCount = stats.reviewRequiredEdgeCount || loadedReview;
  if (reviewCount > 0) items.push({ key: "review", label: "待确认", count: reviewCount });
  if (edges.some((edge) => edge.structural)) items.push({ key: "structural", label: "章节承接", count: null });
  return items;
});

const selectedWikiDetailEntry = computed<StoryWikiEntry | null>(() => {
  const node = selectedWikiNode.value;
  if (!node) {
    return wikiDetailPinned.value ? selectedWikiEntry.value : null;
  }
  if (!node.entryId) {
    return null;
  }
  return wikiEntries.value.find((entry) => entry.id === node.entryId) ?? null;
});

// 条目列表的高亮与详情栏一致：选中节点时跟随节点，否则只认用户明确选中的条目。
const wikiActiveEntryId = computed(() => {
  if (selectedWikiEdgeId.value) {
    return "";
  }
  if (selectedWikiNode.value) {
    return selectedWikiNode.value.entryId || "";
  }
  return wikiDetailPinned.value ? selectedWikiEntry.value?.id || "" : "";
});

const selectedWikiDetailKicker = computed(() => {
  const node = selectedWikiNode.value;
  if (node) {
    const labels = wikiData.value?.nodeTypeLabels ?? {};
    return labels[node.type] || node.type;
  }
  return selectedWikiEntry.value?.categoryLabel || "条目";
});

const wikiInspectorEmptyTitle = computed(() => `${selectedWikiCategoryLabel.value}图谱`);

const wikiInspectorEmptyHint = computed(() => {
  if (wikiGraphSearchQuery.value) {
    return `正在展示“${wikiGraphSearchQuery.value}”的搜索结果，点击节点或连线查看详情。`;
  }
  if (selectedWikiCategory.value === "characters") {
    return "展示独立角色卡及角色之间的关系。仅在正文或关系说明中提及的人物需要补充角色卡；跨分类关系可在“全部”中查看。";
  }
  if (selectedWikiCategory.value === "all") {
    return "展示全部故事条目及跨分类关系。点击节点查看来源，点击连线查看关系证据。";
  }
  if (selectedWikiCategory.value === "plot") {
    return "章节按叙事顺序串联。点击节点查看章节来源及关联条目。";
  }
  return "展示世界观、地点、物品、势力与伏笔。点击节点查看它的证据来源与关联章节。";
});

const wikiGenerationModeLabel = computed(() => {
  const mode = wikiData.value?.generationMode || wikiData.value?.generator || "local evidence-grounded";
  return `生成方式: ${mode}`;
});

const wikiUpdatedAtLabel = computed(() => {
  const value = wikiData.value?.lastUpdatedAt || wikiData.value?.generatedAt;
  return value ? `更新: ${formatTime(value)}` : "尚未生成";
});

const wikiGraphStats = computed(() => {
  const total = wikiGraphQueryData.value?.total ?? {
    entryCount: 0,
    nodeCount: 0,
    edgeCount: 0,
    confirmedEdgeCount: 0,
    reviewRequiredEdgeCount: 0,
    connectedNodeCount: 0,
    isolatedNodeCount: 0,
  };
  const returnedNodeCount = wikiGraphQueryData.value?.returnedNodeCount
    ?? wikiGraphQueryData.value?.pagination?.returnedNodeCount
    ?? wikiGraphQueryData.value?.graph?.nodes?.length
    ?? 0;
  return {
    entryCount: Number(total.entryCount || 0),
    nodeCount: Number(total.nodeCount || 0),
    edgeCount: Number(total.edgeCount || 0),
    confirmedEdgeCount: Number(total.confirmedEdgeCount || 0),
    reviewRequiredEdgeCount: Number(total.reviewRequiredEdgeCount || 0),
    connectedNodeCount: Number(total.connectedNodeCount || 0),
    isolatedNodeCount: Number(total.isolatedNodeCount || 0),
    returnedNodeCount: Number(returnedNodeCount),
    notLoadedNodeCount: Math.max(0, Number(total.nodeCount || 0) - Number(returnedNodeCount)),
  };
});

const wikiNeedsReviewCount = computed(() => (
  wikiReviewTotal.value || wikiGraphStats.value.reviewRequiredEdgeCount ||
  wikiEntries.value.filter((entry) => entry.needsReview).length
));

const selectedWikiReviewCandidate = computed<WikiReviewCandidate | null>(() => {
  const edge = selectedWikiRelationEdge.value;
  if (!edge) {
    return null;
  }
  return wikiReviewQueue.value.find((candidate) => (
    (edge.fingerprint && candidate.fingerprint === edge.fingerprint)
    || (edge.reviewStatus === "review_required"
      && candidate.subjectId === edge.source.id
      && candidate.objectId === edge.target.id
      && candidate.predicate === edge.label)
  )) ?? null;
});

function wikiReviewDraft(candidate: WikiReviewCandidate): WikiReviewDraft {
  const existing = wikiReviewDrafts.value[candidate.id];
  if (existing) {
    return existing;
  }
  const draft: WikiReviewDraft = {
    subjectId: String(candidate.subjectId || ""),
    predicate: String(candidate.predicate || ""),
    objectId: String(candidate.objectId || ""),
    targetSourcePath: String(candidate.targetSourcePath || ""),
    rejectReason: "incorrect",
    rejectNote: "",
  };
  wikiReviewDrafts.value = { ...wikiReviewDrafts.value, [candidate.id]: draft };
  return draft;
}

// 项目总览摘要：overview 分类不再是可见 tab，它的内容改在检查器头部常驻一行。
const wikiProjectSummary = computed(() => {
  const overviewEntry = wikiEntries.value.find((entry) => entry.category === "overview");
  return String(overviewEntry?.summary || wikiData.value?.summary || "").trim();
});

const selectedWikiSourceLabel = computed(() => {
  const generator = wikiData.value?.generator || "";
  if (!generator) {
    return "";
  }
  // storydex-rust-knowledge 是本地确定性投影；Agent 整理的内容也会经过它重建，来源仍是本地证据。
  const mode = String(wikiData.value?.generationMode || "").toLowerCase();
  return LOCAL_WIKI_GENERATORS.has(generator) || mode.includes("local")
    ? "本地证据"
    : "Agent 整理";
});

function formatWikiShortTime(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (part: number) => String(part).padStart(2, "0");
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const sameYear = date.getFullYear() === new Date().getFullYear();
  const day = `${date.getMonth() + 1}/${date.getDate()}`;
  return sameYear ? `${day} ${time}` : `${date.getFullYear()}/${day} ${time}`;
}

// 页头副标题：项目 · 来源 · 更新时间；项目摘要与生成方式放在悬停提示里。
const wikiHeaderMeta = computed(() => {
  if (!wikiData.value) {
    return "";
  }
  const updated = formatWikiShortTime(wikiData.value.lastUpdatedAt || wikiData.value.generatedAt);
  return [
    wikiData.value.projectName,
    selectedWikiSourceLabel.value,
    updated ? `更新于 ${updated}` : "尚未生成",
  ].filter(Boolean).join(" · ");
});

const wikiHeaderTooltip = computed(() => (
  [wikiProjectSummary.value, wikiGenerationModeLabel.value, wikiUpdatedAtLabel.value].filter(Boolean).join("\n")
));

// 详情栏聚焦的节点：显式选中的节点，或当前条目对应的节点。
const wikiFocusNode = computed<WikiGraphNode | null>(() => {
  if (selectedWikiNode.value) {
    return selectedWikiNode.value;
  }
  const entryId = selectedWikiDetailEntry.value?.id;
  return entryId ? wikiGraphNodes.value.find((node) => node.entryId === entryId) ?? null : null;
});

// 当前节点的关系清单（不含章节承接等结构边），待确认的排在前面。
const wikiFocusRelations = computed(() => {
  const node = wikiFocusNode.value;
  if (!node) {
    return [];
  }
  return wikiGraphEdges.value
    .filter((edge) => !edge.structural && (edge.source.id === node.id || edge.target.id === node.id))
    .map((edge) => {
      const outgoing = edge.source.id === node.id;
      const other = outgoing ? edge.target : edge.source;
      return {
        id: edge.id,
        // 与图上关系名一致：先写关系类别，原始谓词不同时作补充。
        category: edge.displayLabel,
        predicate: edge.label,
        outgoing,
        otherLabel: other.label,
        otherTone: other.tone,
        needsReview: edge.reviewStatus === "review_required",
      };
    })
    .sort((a, b) => Number(b.needsReview) - Number(a.needsReview));
});

const selectedWikiDetailSummary = computed(() => cleanWikiSummary(
  selectedWikiNode.value?.summary || selectedWikiDetailEntry.value?.summary || "",
  selectedWikiNode.value?.label || selectedWikiDetailEntry.value?.title || "",
));

// 「来源: path」行与下方来源列表重复，详情里不再重复展示。
const selectedWikiDetailLines = computed(() => {
  const entry = selectedWikiDetailEntry.value;
  const sources = new Set((entry?.sourcePaths ?? []).map((path) => path.trim()));
  return (entry?.details ?? [])
    .map((detail) => String(detail || "").replace(/^[-*•]\s+/, "").trim())
    .filter((detail) => {
      const match = detail.match(/^来源\s*[:：]\s*(.+)$/);
      return detail && !(match && sources.has(match[1].trim()));
    })
    .slice(0, 18);
});

const wikiWorkflowLabel = computed(() => {
  const workflow = wikiData.value?.lastWorkflow || "read";
  const status = wikiData.value?.lastWorkflowStatus || "ready";
  return `${workflow} / ${status}`;
});

interface WikiGraphQueryParams {
  q?: string;
  category?: StoryWikiCategory;
  entryId?: string;
  nodeId?: string;
  depth?: number;
  offset?: number;
  includeReview?: boolean;
}

const FIELD_LABELS: Record<string, string> = {
  character_updates: "角色",
  variable_updates: "变量",
  event_updates: "事件",
  memory_updates: "记忆",
  worldbook_updates: "世界",
  relationship_updates: "关系",
  foreshadow_updates: "伏笔",
  timeline_updates: "时间",
  chapter_outline_updates: "大纲",
};

function shortFieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key;
}

function formatTime(value?: string): string {
  if (!value) return "";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return value;
  }
}

function levelWidth(level: number): string {
  const pct = Math.min(100, Math.abs(level) * 10);
  return `${pct}%`;
}

function levelColor(level: number): string {
  if (level > 0) return "var(--success-fg)";
  if (level < 0) return "var(--danger-fg)";
  return "var(--text-muted)";
}

const RELATIONSHIP_DIMENSION_LABELS: Record<string, string> = {
  trust: "信任",
  intimacy: "亲密",
  hostility: "敌对",
  loyalty: "忠诚",
  alliance: "同盟",
  rivalry: "竞争",
  family: "家族",
  professional: "职业",
};

function formatRelationshipDimension(value: unknown): string {
  const key = normalizeRelationshipGraphText(value).toLowerCase();
  return RELATIONSHIP_DIMENSION_LABELS[key] ?? (normalizeRelationshipGraphText(value) || "关系");
}

function relationshipDimensionLabel(edge: RelationshipEdge): string {
  return formatRelationshipDimension(edge.dimension);
}

function formatRelationshipNodeKind(value: unknown): string {
  const key = normalizeRelationshipGraphText(value).toLowerCase();
  if (key === "character") {
    return "角色";
  }
  return normalizeRelationshipGraphText(value) || "角色";
}

function normalizeRelationshipGraphText(value: unknown): string {
  return String(value ?? "").trim().replace(/\s+/g, " ");
}

function relationshipNodePayloadId(node: RelationshipGraphNodePayload): string {
  return normalizeRelationshipGraphText(node.id ?? node.label ?? node.name ?? "");
}

function relationshipNodePayloadLabel(node: RelationshipGraphNodePayload | undefined, fallback: string): string {
  return normalizeRelationshipGraphText(node?.label ?? node?.name ?? fallback) || fallback;
}

function shortNodeLabel(value: string): string {
  const trimmed = String(value || "").trim();
  if (trimmed.length <= 5) {
    return trimmed;
  }
  return `${trimmed.slice(0, 4)}…`;
}

function buildRelationshipEdgePath(source: RelationshipGraphNode, target: RelationshipGraphNode, index: number): string {
  const curve = buildRelationshipCurve(source, target, index);
  return `M ${source.x} ${source.y} C ${curve.controlAX} ${curve.controlAY}, ${curve.controlBX} ${curve.controlBY}, ${target.x} ${target.y}`;
}

function buildRelationshipEdgeLabelPoint(
  source: RelationshipGraphNode,
  target: RelationshipGraphNode,
  index: number
): { x: number; y: number } {
  const curve = buildRelationshipCurve(source, target, index);
  return {
    x: (source.x + 3 * curve.controlAX + 3 * curve.controlBX + target.x) / 8,
    y: (source.y + 3 * curve.controlAY + 3 * curve.controlBY + target.y) / 8,
  };
}

function buildRelationshipCurve(source: RelationshipGraphNode, target: RelationshipGraphNode, index: number) {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.max(1, Math.hypot(dx, dy));
  const normalX = -dy / distance;
  const normalY = dx / distance;
  const offsetDirection = index % 2 === 0 ? 1 : -1;
  const offset = offsetDirection * (34 + (index % 3) * 12);
  const drift = ((index % 5) - 2) * 4;

  return {
    controlAX: source.x + dx * 0.33 + normalX * offset + drift,
    controlAY: source.y + dy * 0.33 + normalY * offset - drift,
    controlBX: source.x + dx * 0.67 + normalX * offset - drift,
    controlBY: source.y + dy * 0.67 + normalY * offset + drift,
  };
}

function zoomRelationshipGraph(delta: number): void {
  relationshipGraphZoom.value = clampNumber(relationshipGraphZoom.value + delta, 0.65, 1.8);
}

function resetRelationshipGraphView(): void {
  relationshipGraphZoom.value = 1;
  relationshipGraphPan.value = { x: 0, y: 0 };
  relationshipNodeOverrides.value = {};
}

function selectRelationshipEdge(index: number): void {
  selectedRelationshipIndex.value = index;
  selectedRelationshipNodeId.value = "";
}

function selectRelationshipNode(nodeId: string): void {
  selectedRelationshipNodeId.value = nodeId;
}

function handleRelationshipGraphWheel(event: WheelEvent): void {
  zoomRelationshipGraph(event.deltaY > 0 ? -0.08 : 0.08);
}

function beginRelationshipNodeDrag(event: PointerEvent, nodeId: string): void {
  const point = relationshipGraphPointFromEvent(event);
  const node = relationshipGraphNodes.value.find((item) => item.id === nodeId);
  if (!point || !node) {
    return;
  }
  relationshipDragState.value = {
    nodeId,
    pointerId: event.pointerId,
    offsetX: point.x - node.x,
    offsetY: point.y - node.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function beginRelationshipGraphPan(event: PointerEvent): void {
  if (event.button !== 0) {
    return;
  }
  const point = relationshipGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  event.preventDefault();
  relationshipPanState.value = {
    pointerId: event.pointerId,
    startX: point.x,
    startY: point.y,
    originX: relationshipGraphPan.value.x,
    originY: relationshipGraphPan.value.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function moveRelationshipGraphPointer(event: PointerEvent): void {
  const drag = relationshipDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    const point = relationshipGraphPointFromEvent(event);
    if (!point) {
      return;
    }
    relationshipNodeOverrides.value = {
      ...relationshipNodeOverrides.value,
      [drag.nodeId]: {
        x: clampNumber(point.x - drag.offsetX, 58, RELATION_GRAPH_WIDTH - 58),
        y: clampNumber(point.y - drag.offsetY, 54, RELATION_GRAPH_HEIGHT - 54),
      },
    };
    return;
  }

  const pan = relationshipPanState.value;
  if (!pan || pan.pointerId !== event.pointerId) {
    return;
  }
  const point = relationshipGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  relationshipGraphPan.value = {
    x: pan.originX + point.x - pan.startX,
    y: pan.originY + point.y - pan.startY,
  };
}

function endRelationshipGraphPointer(event: PointerEvent): void {
  const drag = relationshipDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    relationshipDragState.value = null;
  }
  const pan = relationshipPanState.value;
  if (pan && pan.pointerId === event.pointerId) {
    relationshipPanState.value = null;
  }
}

function relationshipGraphRawPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const svg = relationshipGraphSvgRef.value;
  if (!svg) {
    return null;
  }
  const rect = svg.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  const rawX = ((event.clientX - rect.left) / rect.width) * RELATION_GRAPH_WIDTH;
  const rawY = ((event.clientY - rect.top) / rect.height) * RELATION_GRAPH_HEIGHT;
  return { x: rawX, y: rawY };
}

function relationshipGraphPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const raw = relationshipGraphRawPointFromEvent(event);
  if (!raw) {
    return null;
  }
  return {
    x: (raw.x - relationshipGraphPan.value.x) / relationshipGraphZoom.value,
    y: (raw.y - relationshipGraphPan.value.y) / relationshipGraphZoom.value,
  };
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

async function loadSnapshot() {
  const requestSeq = ++snapshotLoadSeq;
  loading.value = true;
  errorMessage.value = "";
  try {
    const response = await apiClient.get<ApiEnvelope<EvolutionSnapshot>>("/story/evolution-snapshot");
    const data = unwrapEnvelope(response.data, "Story evolution snapshot request failed.");
    if (requestSeq !== snapshotLoadSeq) {
      return;
    }
    snapshot.value = data.data ?? {};
    if (selectedRelationshipIndex.value >= relationshipEdges.value.length) {
      selectedRelationshipIndex.value = 0;
    }
  } catch (error: unknown) {
    if (requestSeq !== snapshotLoadSeq) {
      return;
    }
    errorMessage.value = (error as Error)?.message || "无法读取项目状态。";
  } finally {
    if (requestSeq === snapshotLoadSeq) {
      loading.value = false;
    }
  }
}

function currentWikiGraphQueryParams(): WikiGraphQueryParams {
  if (wikiGraphSearchQuery.value.trim()) {
    return { q: wikiGraphSearchQuery.value.trim(), includeReview: true };
  }
  // 节点选择只控制详情和高亮；同步、分页继续使用当前分类或搜索范围。
  return { category: selectedWikiCategory.value, includeReview: true };
}

function mergeWikiGraphPage(
  previous: StoryWikiGraphQueryResponse | null,
  page: StoryWikiGraphQueryResponse,
): StoryWikiGraphQueryResponse {
  if (!previous) {
    return page;
  }
  const nodesById = new Map<string, StoryWikiNodePayload>();
  for (const node of [...(previous.graph?.nodes || []), ...(page.graph?.nodes || [])]) {
    if (node?.id) {
      nodesById.set(node.id, { ...nodesById.get(node.id), ...node });
    }
  }
  const edgesByKey = new Map<string, StoryWikiEdgePayload>();
  for (const edge of [...(previous.graph?.edges || []), ...(page.graph?.edges || [])]) {
    if (!edge?.source || !edge?.target) {
      continue;
    }
    const key = String(edge.id || `${edge.source}|${edge.target}|${edge.predicate || edge.label || edge.type}`);
    edgesByKey.set(key, { ...edgesByKey.get(key), ...edge });
  }
  const entriesById = new Map<string, StoryWikiEntry>();
  for (const entry of [...(previous.entries || []), ...(page.entries || [])]) {
    if (entry?.id) {
      entriesById.set(entry.id, { ...entriesById.get(entry.id), ...entry });
    }
  }
  return {
    ...previous,
    ...page,
    entries: Array.from(entriesById.values()),
    graph: {
      ...page.graph,
      nodes: Array.from(nodesById.values()),
      edges: Array.from(edgesByKey.values()),
    },
    total: page.total || previous.total,
    offset: page.offset ?? previous.offset,
    returnedNodeCount: Array.from(nodesById.values()).length,
    hasMore: Boolean(page.hasMore),
    nextOffset: page.nextOffset ?? null,
    pagination: page.pagination,
  };
}

async function loadWikiGraph(
  params: WikiGraphQueryParams = currentWikiGraphQueryParams(),
  options: { append?: boolean; fit?: boolean } = {},
): Promise<void> {
  const append = Boolean(options.append);
  const requestSeq = ++wikiGraphLoadSeq;
  wikiGraphLoading.value = true;
  const queryParams: Record<string, string | number> = {
    depth: params.depth ?? (params.nodeId ? 1 : 1),
    limit: 60,
    offset: Math.max(0, Number(params.offset ?? 0)),
    includeReview: params.includeReview === false ? "false" : "true",
  };
  if (params.nodeId) {
    queryParams.nodeId = params.nodeId;
  } else if (params.entryId) {
    queryParams.entryId = params.entryId;
  } else if (params.q?.trim()) {
    queryParams.q = params.q.trim();
  } else {
    queryParams.category = normalizeWikiCategory(params.category || selectedWikiCategory.value);
  }

  try {
    const response = await apiClient.get<ApiEnvelope<StoryWikiGraphQueryResponse>>("/story/wiki/graph", {
      params: queryParams,
    });
    const data = unwrapEnvelope(response.data, "Story wiki graph query failed.");
    if (requestSeq !== wikiGraphLoadSeq) {
      return;
    }
    const page = data.data ?? null;
    if (page && (["error", "unavailable", "stale", "rebuilding"].includes(String(page.status)) || page.queryStatus === "unavailable")) {
      throw new Error("知识图谱尚未成功生成，请检查同步错误后重试。");
    }
    wikiErrorMessage.value = "";
    wikiGraphQueryData.value = append && page
      ? mergeWikiGraphPage(wikiGraphQueryData.value, page)
      : page;
    wikiGraphHasMore.value = Boolean(page?.hasMore ?? page?.pagination?.hasMore);
    wikiGraphNextOffset.value = page?.nextOffset
      ?? page?.pagination?.nextOffset
      ?? null;
    ensureWikiGraphSelection();
    if (options.fit && !append) {
      // 切换分类或搜索后布局整体重排，沿用上一个视图的缩放与平移会偏出画面。
      await nextTick();
      if (requestSeq === wikiGraphLoadSeq) {
        fitWikiGraphView();
      }
    }
  } catch (error: unknown) {
    if (requestSeq === wikiGraphLoadSeq) {
      wikiErrorMessage.value = describeTransportError(error, "知识图谱查询失败。");
      if (!append) {
        wikiGraphQueryData.value = null;
      }
    }
  } finally {
    if (requestSeq === wikiGraphLoadSeq) {
      wikiGraphLoading.value = false;
    }
  }
}

async function loadMoreWikiGraph(): Promise<void> {
  if (wikiGraphLoading.value || !wikiGraphHasMore.value || wikiGraphNextOffset.value === null) {
    return;
  }
  await loadWikiGraph(
    { ...currentWikiGraphQueryParams(), offset: wikiGraphNextOffset.value, includeReview: true },
    { append: true },
  );
}

async function loadWikiReviewQueue(): Promise<void> {
  wikiReviewLoading.value = true;
  wikiReviewError.value = "";
  try {
    const response = await apiClient.get<ApiEnvelope<{
      relations: WikiReviewCandidate[];
      total: number;
      offset: number;
      limit: number;
      hasMore: boolean;
      nextOffset?: number | null;
    }>>("/story/wiki/relations/review", {
      params: { status: "review_required", offset: 0, limit: 500 },
    });
    const data = unwrapEnvelope(response.data, "Story wiki relation review request failed.");
    const payload = data.data;
    wikiReviewQueue.value = Array.isArray(payload?.relations) ? payload.relations : [];
    wikiReviewTotal.value = Number(payload?.total || wikiReviewQueue.value.length);
    for (const candidate of wikiReviewQueue.value) {
      wikiReviewDraft(candidate);
    }
  } catch (error: unknown) {
    wikiReviewError.value = describeWikiRequestError(error, "无法读取关系待确认队列。");
  } finally {
    wikiReviewLoading.value = false;
  }
}

function toggleWikiReviewQueue(): void {
  wikiReviewOpen.value = !wikiReviewOpen.value;
  if (wikiReviewOpen.value) {
    wikiInspectorOpen.value = true;
  }
  if (wikiReviewOpen.value || wikiNeedsReviewCount.value > 0) {
    void loadWikiReviewQueue();
  }
}

async function openWikiSource(path: string): Promise<void> {
  const normalized = String(path || "").trim().replace(/\\/g, "/");
  if (!normalized) {
    return;
  }
  try {
    await workspaceStore.openFile(normalized);
    // 图谱页没有编辑区：打开成功后切回资源视图，否则用户看不到打开的文件。
    if (props.relationshipOnly && workspaceStore.activeFile === normalized) {
      uiStore.setActivity("resources");
    }
  } catch (error: unknown) {
    wikiReviewError.value = describeTransportError(error, "无法打开来源文件。");
  }
}

async function confirmWikiCandidate(candidate: WikiReviewCandidate): Promise<void> {
  const draft = wikiReviewDraft(candidate);
  if (!candidate.id || !candidate.fingerprint) {
    wikiReviewError.value = "候选缺少 fingerprint，无法安全确认。";
    return;
  }
  wikiReviewActionId.value = candidate.id;
  wikiReviewError.value = "";
  try {
    const response = await apiClient.post<ApiEnvelope<unknown>>(
      `/story/wiki/relations/${encodeURIComponent(candidate.id)}/confirm`,
      {
        expectedFingerprint: candidate.fingerprint,
        subjectId: draft.subjectId,
        predicate: draft.predicate,
        objectId: draft.objectId,
        targetSourcePath: draft.targetSourcePath,
      },
    );
    unwrapEnvelope(response.data, "确认关系候选失败。");
    await loadWiki();
  } catch (error: unknown) {
    wikiReviewError.value = describeWikiRequestError(error, "确认关系候选失败。");
  } finally {
    wikiReviewActionId.value = "";
  }
}

async function rejectWikiCandidate(candidate: WikiReviewCandidate): Promise<void> {
  const draft = wikiReviewDraft(candidate);
  if (!candidate.id || !candidate.fingerprint) {
    wikiReviewError.value = "候选缺少 fingerprint，无法安全驳回。";
    return;
  }
  wikiReviewActionId.value = candidate.id;
  wikiReviewError.value = "";
  try {
    const response = await apiClient.post<ApiEnvelope<unknown>>(
      `/story/wiki/relations/${encodeURIComponent(candidate.id)}/reject`,
      {
        expectedFingerprint: candidate.fingerprint,
        reason: draft.rejectReason || "other",
        note: draft.rejectNote,
      },
    );
    unwrapEnvelope(response.data, "驳回关系候选失败。");
    await loadWiki();
  } catch (error: unknown) {
    wikiReviewError.value = describeWikiRequestError(error, "驳回关系候选失败。");
  } finally {
    wikiReviewActionId.value = "";
  }
}

async function loadWiki(): Promise<void> {
  wikiLoading.value = true;
  wikiErrorMessage.value = "";
  try {
    const response = await apiClient.get<ApiEnvelope<StoryWikiData>>("/story/wiki");
    const data = unwrapEnvelope(response.data, "Story wiki request failed.");
    wikiData.value = data.data ?? null;
    ensureWikiSelection();
    await Promise.all([
      loadWikiGraph({ category: selectedWikiCategory.value, includeReview: true }),
      loadWikiReviewQueue(),
    ]);
  } catch (error: unknown) {
    wikiErrorMessage.value = describeTransportError(error, "无法读取知识图谱。");
  } finally {
    wikiLoading.value = false;
  }
}

async function rebuildWiki(): Promise<void> {
  wikiRebuilding.value = true;
  wikiErrorMessage.value = "";
  try {
    const response = await apiClient.post<ApiEnvelope<StoryWikiData>>("/story/wiki/rebuild");
    const data = unwrapEnvelope(response.data, "Story wiki rebuild request failed.");
    wikiData.value = data.data ?? null;
    ensureWikiSelection();
    await Promise.all([
      loadWikiGraph(currentWikiGraphQueryParams()),
      loadWikiReviewQueue(),
    ]);
  } catch (error: unknown) {
    wikiErrorMessage.value = describeTransportError(error, "知识图谱重新生成失败。");
  } finally {
    wikiRebuilding.value = false;
  }
}

function runWikiAgentWorkflow(workflow: "generate" | "update" | "review"): void {
  const prompts = {
    generate: "请深度分析当前 Storydex 小说项目的全部章节、角色、设定、事件和关系，重新生成完整知识图谱与 WIKI。请先检查现有数据，保留有证据的内容，标注冲突与待确认项，并通过 Storydex 项目接口写入结果。",
    update: "请扫描当前 Storydex 小说项目自上次 WIKI 更新后的所有改动，增量更新知识图谱与 WIKI。不要覆盖未受影响的人工内容；为新增或变化的角色、事件、关系补充来源证据，并标注冲突与待确认项。",
    review: "请全面审阅当前 Storydex 项目的 WIKI 与知识图谱，对照章节原文检查事实、角色、时间线和关系。请列出缺失、矛盾、过期和证据不足的条目，并在确认可靠后通过 Storydex 项目接口修正。"
  } as const;
  agentStore.promptInput = prompts[workflow];
  agentStore.taskMode = "chat";
  uiStore.setAgentCollapsed(false);
  wikiAgentTone.value = "idle";
  wikiAgentStatus.value = "指令已填入 Agent 输入框，可检查或补充后发送。";
  wikiErrorMessage.value = "";
}

function describeWikiRequestError(error: unknown, fallback: string): string {
  const response = error && typeof error === "object"
    ? (error as { response?: { data?: ApiEnvelope<unknown> } }).response
    : undefined;
  const serverMessage = response?.data?.error?.message;
  return serverMessage || describeTransportError(error, fallback);
}

function refreshPanel(): void {
  if (props.relationshipOnly) {
    void loadWiki();
    return;
  }
  void loadSnapshot();
}

const wikiSyncing = ref(false);
let wikiSyncTimer: ReturnType<typeof setTimeout> | null = null;
let wikiSyncPending = false;

async function syncWiki(): Promise<void> {
  if (wikiSyncing.value) {
    wikiSyncPending = true;
    return;
  }
  // 不打断手动 Agent 流程或重建。
  if (wikiRebuilding.value || wikiLoading.value) {
    wikiSyncPending = true;
    return;
  }
  wikiSyncing.value = true;
  const previousSyncError = wikiSyncErrorMessage.value;
  wikiSyncErrorMessage.value = "";
  try {
    const response = await apiClient.post<ApiEnvelope<StoryWikiData>>("/story/wiki/sync");
    const data = unwrapEnvelope(response.data, "Story wiki sync failed.");
    if (data.data) {
      wikiData.value = data.data;
      ensureWikiSelection();
      await Promise.all([
        loadWikiGraph(currentWikiGraphQueryParams()),
        loadWikiReviewQueue(),
      ]);
    }
    if (previousSyncError && wikiAgentStatus.value === previousSyncError) {
      wikiAgentStatus.value = "";
      wikiAgentTone.value = "idle";
    }
  } catch (error: unknown) {
    const reason = describeTransportError(error, "同步请求失败。");
    wikiSyncErrorMessage.value = `知识图谱自动同步失败：${reason}`;
    wikiAgentStatus.value = wikiSyncErrorMessage.value;
    wikiAgentTone.value = "warning";
    if (wikiData.value) {
      wikiData.value = {
        ...wikiData.value,
        status: "stale",
        lastSuccessfulRevision: wikiData.value.lastSuccessfulRevision
          ?? wikiData.value.knowledgeRevision
          ?? 0,
      };
    }
  } finally {
    wikiSyncing.value = false;
    if (wikiSyncPending) {
      wikiSyncPending = false;
      scheduleWikiSync();
    }
  }
}

function scheduleWikiSync(): void {
  if (wikiSyncTimer) {
    clearTimeout(wikiSyncTimer);
  }
  // 防抖：连续保存/写作后静置约 3s 再做一次本地增量同步，避免频繁写盘。
  wikiSyncTimer = setTimeout(() => {
    wikiSyncTimer = null;
    void syncWiki();
  }, 3000);
}

function ensureWikiSelection(): void {
  // 默认角色视图：角色是读者与作者最常回看的一层，其余两类通过 tab 切换。
  selectedWikiCategory.value = normalizeWikiCategory(selectedWikiCategory.value || preferredWikiCategory());
  if (!wikiEntries.value.some((entry) => entry.id === selectedWikiEntryId.value)) {
    selectedWikiEntryId.value = selectedWikiCategoryEntries.value[0]?.id || wikiEntries.value[0]?.id || "";
  }
}

function ensureWikiGraphSelection(): void {
  const visibleEntries = visibleWikiEntries.value;
  if (visibleEntries.length && !visibleEntries.some((entry) => entry.id === selectedWikiEntryId.value)) {
    selectedWikiEntryId.value = visibleEntries[0].id;
    wikiDetailPinned.value = false;
  }
  if (selectedWikiNodeId.value && !wikiGraphNodes.value.some((node) => node.id === selectedWikiNodeId.value)) {
    selectedWikiNodeId.value = "";
  }
  if (selectedWikiEdgeId.value && !wikiGraphEdges.value.some((edge) => edge.id === selectedWikiEdgeId.value)) {
    selectedWikiEdgeId.value = "";
  }
}

function normalizeWikiCategory(value: string | undefined): StoryWikiCategory {
  // 旧数据与旧 URL 里的 relationships 视图已并入角色视图。
  const normalized = value === "relationships" ? "characters" : value;
  return WIKI_CATEGORY_TABS.some((category) => category.id === normalized)
    ? normalized as StoryWikiCategory
    : "characters";
}

function preferredWikiCategory(): StoryWikiCategory {
  // 按条目自身的分类计数，不能走 normalizeWikiCategory——它把未知分类兜底成
  // characters，会让「有没有角色」这个判断永远为真。
  const counts = new Map<string, number>();
  wikiEntries.value.forEach((entry) => {
    const category = String(entry.category ?? "").trim();
    counts.set(category, (counts.get(category) ?? 0) + 1);
  });
  if ((counts.get("characters") ?? 0) > 0) {
    return "characters";
  }
  if ((counts.get("plot") ?? 0) > 0) {
    return "plot";
  }
  return "setting";
}

function hasWikiRelationshipNetwork(): boolean {
  const nodes = wikiData.value?.graph?.nodes ?? [];
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  return (wikiData.value?.graph?.edges ?? []).some((edge) => {
    if (edge.type !== "relationship") {
      return false;
    }
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    return source?.type === "character" && target?.type === "character";
  });
}

function selectWikiCategory(categoryId: StoryWikiCategory): void {
  selectedWikiCategory.value = normalizeWikiCategory(categoryId);
  selectedWikiEntryId.value = wikiEntries.value.find((entry) => entry.category === selectedWikiCategory.value)?.id || "";
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  wikiDetailPinned.value = false;
  wikiGraphSearchInput.value = "";
  wikiGraphSearchQuery.value = "";
  void loadWikiGraph({ category: categoryId }, { fit: true });
}

function selectWikiEntry(entryId: string): void {
  selectedWikiEntryId.value = entryId;
  const node = wikiGraphNodes.value.find((item) => item.entryId === entryId);
  selectedWikiNodeId.value = node?.id || "";
  selectedWikiEdgeId.value = "";
  wikiDetailPinned.value = true;
  wikiInspectorOpen.value = true;
  scheduleWikiSelectionReveal();
}

function isWikiNodeSelectable(node: WikiGraphNode): boolean {
  return node.selectable !== false && !node.synthetic;
}

function selectWikiNode(node: WikiGraphNode): void {
  if (!isWikiNodeSelectable(node)) {
    return;
  }
  selectedWikiNodeId.value = node.id;
  selectedWikiEdgeId.value = "";
  if (node.entryId) {
    selectedWikiEntryId.value = node.entryId;
  }
  wikiDetailPinned.value = true;
  wikiInspectorOpen.value = true;
  scheduleWikiSelectionReveal();
}

function selectWikiEdge(edgeId: string): void {
  selectedWikiEdgeId.value = edgeId;
  selectedWikiNodeId.value = "";
  wikiInspectorOpen.value = true;
  scheduleWikiSelectionReveal();
}

function isWikiEdgeActive(edge: Pick<WikiGraphEdge, "id" | "source" | "target">): boolean {
  if (selectedWikiEdgeId.value) {
    return selectedWikiEdgeId.value === edge.id;
  }
  if (!selectedWikiNodeId.value) {
    return false;
  }
  return edge.source.id === selectedWikiNodeId.value || edge.target.id === selectedWikiNodeId.value;
}

function submitWikiGraphSearch(): void {
  const query = wikiGraphSearchInput.value.trim();
  wikiGraphSearchQuery.value = query;
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  wikiDetailPinned.value = false;
  if (!query) {
    void loadWikiGraph({ category: selectedWikiCategory.value }, { fit: true });
    return;
  }
  void loadWikiGraph({ q: query }, { fit: true });
}

function clearWikiGraphSearch(): void {
  wikiGraphSearchInput.value = "";
  wikiGraphSearchQuery.value = "";
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  wikiDetailPinned.value = false;
  void loadWikiGraph({ category: selectedWikiCategory.value }, { fit: true });
}

function zoomWikiGraphAt(point: { x: number; y: number }, factor: number): void {
  stopWikiPanAnimation();
  const next = clampNumber(wikiGraphZoom.value * factor, 0.35, 2.6);
  const ratio = next / wikiGraphZoom.value;
  wikiGraphPan.value = {
    x: point.x - (point.x - wikiGraphPan.value.x) * ratio,
    y: point.y - (point.y - wikiGraphPan.value.y) * ratio,
  };
  wikiGraphZoom.value = next;
}

function zoomWikiGraphStep(direction: number): void {
  zoomWikiGraphAt(
    { x: wikiCanvasSize.value.width / 2, y: wikiCanvasSize.value.height / 2 },
    direction > 0 ? 1.2 : 1 / 1.2,
  );
}

// 依据节点与标签的包围盒自动适配缩放与平移；顶/底留出悬浮控件的位置，小图不过度放大。
function fitWikiGraphView(): void {
  stopWikiPanAnimation();
  const nodes = wikiGraphNodes.value;
  const { width, height } = wikiCanvasSize.value;
  if (!nodes.length || width <= 0 || height <= 0) {
    wikiGraphZoom.value = 1;
    wikiGraphPan.value = { x: 0, y: 0 };
    return;
  }
  const labels = wikiNodeLabelLayout.value;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  nodes.forEach((node) => {
    minX = Math.min(minX, node.x - node.radius - 6);
    maxX = Math.max(maxX, node.x + node.radius + 6);
    minY = Math.min(minY, node.y - node.radius - 6);
    maxY = Math.max(maxY, node.y + node.radius + 6);
    const rect = labels[node.id]?.rect;
    if (rect) {
      minX = Math.min(minX, rect.x - 4);
      maxX = Math.max(maxX, rect.x + rect.width + 4);
      minY = Math.min(minY, rect.y - 4);
      maxY = Math.max(maxY, rect.y + rect.height + 4);
    }
  });
  wikiLayoutCaptions.value.forEach((caption) => {
    minX = Math.min(minX, caption.x);
    maxX = Math.max(maxX, caption.x + caption.width);
    minY = Math.min(minY, caption.y - 12);
  });
  const inset = { top: wikiGraphHasMore.value ? 52 : 20, right: 20, bottom: 60, left: 20 };
  const availableWidth = Math.max(1, width - inset.left - inset.right);
  const availableHeight = Math.max(1, height - inset.top - inset.bottom);
  const boxWidth = Math.max(1, maxX - minX);
  const boxHeight = Math.max(1, maxY - minY);
  const zoom = clampNumber(Math.min(availableWidth / boxWidth, availableHeight / boxHeight), 0.35, 1.3);
  wikiGraphZoom.value = zoom;
  wikiGraphPan.value = {
    x: inset.left + (availableWidth - boxWidth * zoom) / 2 - minX * zoom,
    y: inset.top + (availableHeight - boxHeight * zoom) / 2 - minY * zoom,
  };
}

function resetWikiGraphView(): void {
  wikiNodeOverrides.value = {};
  recomputeWikiLayout(true);
  fitWikiGraphView();
}

function clearWikiGraphSelection(): void {
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  wikiDetailPinned.value = false;
}

// 窄宽度下详情栏浮在舞台右侧；选中的节点或连线若被它盖住，就把视图平移到剩余的可见区域里。
let wikiPanAnimationFrame = 0;

function stopWikiPanAnimation(): void {
  if (wikiPanAnimationFrame && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(wikiPanAnimationFrame);
  }
  wikiPanAnimationFrame = 0;
}

function animateWikiGraphPan(target: { x: number; y: number }): void {
  stopWikiPanAnimation();
  const reduceMotion = typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion || typeof requestAnimationFrame !== "function") {
    wikiGraphPan.value = target;
    return;
  }
  const from = { ...wikiGraphPan.value };
  const startedAt = performance.now();
  const duration = 220;
  const step = (now: number) => {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    wikiGraphPan.value = {
      x: from.x + (target.x - from.x) * eased,
      y: from.y + (target.y - from.y) * eased,
    };
    wikiPanAnimationFrame = progress < 1 ? requestAnimationFrame(step) : 0;
  };
  wikiPanAnimationFrame = requestAnimationFrame(step);
}

function revealWikiPoint(point: { x: number; y: number }): void {
  const panel = wikiGraphPanelRef.value;
  const inspector = wikiInspectorRef.value;
  if (!panel || !inspector || typeof window === "undefined" || window.getComputedStyle(inspector).position !== "absolute") {
    return;
  }
  const stageRect = panel.getBoundingClientRect();
  const inspectorRect = inspector.getBoundingClientRect();
  const { width, height } = wikiCanvasSize.value;
  if (stageRect.width <= 0 || stageRect.height <= 0 || width <= 0 || height <= 0) {
    return;
  }
  // viewBox 按 meet 缩放并居中：把详情栏左缘换算成画布坐标。
  const scale = Math.min(stageRect.width / width, stageRect.height / height);
  const offsetX = (stageRect.width - width * scale) / 2;
  const visibleRight = (inspectorRect.left - stageRect.left - offsetX) / scale - 24;
  const zoom = wikiGraphZoom.value;
  const screenX = wikiGraphPan.value.x + point.x * zoom;
  const screenY = wikiGraphPan.value.y + point.y * zoom;
  const target = { ...wikiGraphPan.value };
  if (screenX > visibleRight || screenX < 24) {
    target.x += Math.max(24, visibleRight / 2) - screenX;
  }
  if (screenY < 32 || screenY > height - 64) {
    target.y += height / 2 - screenY;
  }
  if (Math.abs(target.x - wikiGraphPan.value.x) > 1 || Math.abs(target.y - wikiGraphPan.value.y) > 1) {
    animateWikiGraphPan(target);
  }
}

function scheduleWikiSelectionReveal(): void {
  void nextTick(() => {
    const node = selectedWikiNode.value;
    if (node) {
      revealWikiPoint({ x: node.x, y: node.y });
      return;
    }
    const edge = selectedWikiRelationEdge.value;
    if (edge) {
      revealWikiPoint(pointOnWikiEdge(edge.geometry, 0.5));
    }
  });
}

// 点击空白画布取消选择并收起浮动详情栏；拖动平移后的那次 click 不算。
function handleWikiGraphBackgroundClick(): void {
  if (wikiGraphPanMoved) {
    wikiGraphPanMoved = false;
    return;
  }
  clearWikiGraphSelection();
  wikiInspectorOpen.value = false;
}

function wikiGraphRawPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const svg = wikiGraphSvgRef.value;
  if (!svg) {
    return null;
  }
  const rect = svg.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  return {
    x: ((event.clientX - rect.left) / rect.width) * wikiCanvasSize.value.width,
    y: ((event.clientY - rect.top) / rect.height) * wikiCanvasSize.value.height,
  };
}

function wikiGraphPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const raw = wikiGraphRawPointFromEvent(event);
  if (!raw) {
    return null;
  }
  return {
    x: (raw.x - wikiGraphPan.value.x) / wikiGraphZoom.value,
    y: (raw.y - wikiGraphPan.value.y) / wikiGraphZoom.value,
  };
}

function handleWikiGraphWheel(event: WheelEvent): void {
  const point = wikiGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  // 以鼠标位置为锚点缩放，保持指针下的内容不漂移。
  zoomWikiGraphAt(point, event.deltaY > 0 ? 1 / 1.12 : 1.12);
}

function beginWikiGraphPan(event: PointerEvent): void {
  if (event.button !== 0) {
    return;
  }
  const point = wikiGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  event.preventDefault();
  stopWikiPanAnimation();
  wikiGraphPanMoved = false;
  wikiGraphPanState.value = {
    pointerId: event.pointerId,
    startX: point.x,
    startY: point.y,
    originX: wikiGraphPan.value.x,
    originY: wikiGraphPan.value.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function beginWikiNodeDrag(event: PointerEvent, node: WikiGraphNode): void {
  const point = wikiGraphPointFromEvent(event);
  if (!point) {
    return;
  }
  wikiGraphDragState.value = {
    nodeId: node.id,
    pointerId: event.pointerId,
    offsetX: point.x - node.x,
    offsetY: point.y - node.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function moveWikiGraphPointer(event: PointerEvent): void {
  const drag = wikiGraphDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    const point = wikiGraphPointFromEvent(event);
    if (!point) {
      return;
    }
    wikiNodeOverrides.value = {
      ...wikiNodeOverrides.value,
      [drag.nodeId]: {
        x: point.x - drag.offsetX,
        y: point.y - drag.offsetY,
      },
    };
    return;
  }
  const pan = wikiGraphPanState.value;
  if (!pan || pan.pointerId !== event.pointerId) {
    return;
  }
  const point = wikiGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  if (Math.hypot(point.x - pan.startX, point.y - pan.startY) > 3) {
    wikiGraphPanMoved = true;
  }
  wikiGraphPan.value = {
    x: pan.originX + point.x - pan.startX,
    y: pan.originY + point.y - pan.startY,
  };
}

function endWikiGraphPointer(event: PointerEvent): void {
  const drag = wikiGraphDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    wikiGraphDragState.value = null;
  }
  const pan = wikiGraphPanState.value;
  if (pan && pan.pointerId === event.pointerId) {
    wikiGraphPanState.value = null;
  }
}

async function resolveConflict(idx: number, decision: "accept_incoming" | "keep_existing" | "dismiss") {
  resolving.value = idx;
  try {
    await apiClient.post<ApiEnvelope<unknown>>("/story/character-conflicts/resolve", {
      entryIndex: idx,
      decision,
    });
    await loadSnapshot();
  } catch (error: unknown) {
    errorMessage.value = (error as Error)?.message || "裁定提交失败。";
  } finally {
    resolving.value = null;
  }
}

function entryHasSegmentPath(entry: ChangeEntry): boolean {
  if (!entry.written || entry.written.length === 0) {
    return false;
  }
  return entry.written.some((p) => /^chapters\/.+\.(md|txt)$/i.test(p));
}

async function rollbackToEntry(entry: ChangeEntry, idx: number) {
  if (!entryHasSegmentPath(entry)) {
    return;
  }
  const segmentPath = (entry.written || []).find((p) => /^chapters\/.+\.(md|txt)$/i.test(p));
  if (!segmentPath) {
    return;
  }
  const confirmText = `确认回档到该段之前？\n该段 (${segmentPath}) 及之后的所有正文与变量快照会被备份后删除。\n可点击 toast 中的"撤销回档"恢复。`;
  // eslint-disable-next-line no-alert
  if (!window.confirm(confirmText)) {
    return;
  }
  rollingBack.value = idx;
  try {
    const response = await apiClient.post<ApiEnvelope<{ ok: boolean; rollbackId?: string; deletedSegmentCount?: number; reason?: string }>>(
      "/story/rollback",
      { target_segment_relative_path: segmentPath, keep_target: true },
    );
    const data = unwrapEnvelope(response.data, "Rollback request failed.");
    const payload = data.data;
    if (payload?.ok && payload.rollbackId) {
      lastRollbackId.value = payload.rollbackId;
      // eslint-disable-next-line no-alert
      window.alert(`已回档。删除片段数: ${payload.deletedSegmentCount ?? 0}\n备份 ID: ${payload.rollbackId}\n如需撤销，请点击下方 "撤销最近回档"。`);
    } else {
      errorMessage.value = `回档失败：${payload?.reason ?? "未知原因"}`;
    }
    await loadSnapshot();
  } catch (error: unknown) {
    errorMessage.value = (error as Error)?.message || "回档请求失败。";
  } finally {
    rollingBack.value = null;
  }
}

async function undoLastRollback() {
  if (!lastRollbackId.value) {
    return;
  }
  try {
    await apiClient.post<ApiEnvelope<unknown>>("/story/rollback/undo", { rollbackId: lastRollbackId.value });
    lastRollbackId.value = "";
    await loadSnapshot();
  } catch (error: unknown) {
    errorMessage.value = (error as Error)?.message || "撤销回档失败。";
  }
}

function isCharacterAssetPath(value: unknown): boolean {
  const normalized = String(value || "").trim().replace(/\\/g, "/");
  return /^\.storydex\/characters\/.+\.(json|md|txt)$/i.test(normalized);
}

watch(
  () => storyStore.lastLoadedAt,
  (next, previous) => {
    if (props.relationshipOnly || !next || next === previous) {
      return;
    }
    void loadSnapshot();
  }
);

watch(
  () => [workspaceStore.lastSavedAt, workspaceStore.activeFile] as const,
  ([nextSavedAt, activeFile], [previousSavedAt]) => {
    if (props.relationshipOnly || !nextSavedAt || nextSavedAt === previousSavedAt || !isCharacterAssetPath(activeFile)) {
      return;
    }
    void loadSnapshot();
  }
);

watch(
  () => activeTab.value,
  (next, previous) => {
    if (props.relationshipOnly || next !== "relations" || next === previous) {
      return;
    }
    void loadSnapshot();
  }
);

watch(
  () => wikiGraphQueryData.value,
  (next, previous) => {
    const survivingIds = new Set((next?.graph?.nodes ?? []).map((node) => node.id));
    wikiNodeOverrides.value = Object.fromEntries(Object.entries(wikiNodeOverrides.value)
      .filter(([id]) => survivingIds.has(id)));
    hoveredWikiNodeId.value = "";
    hoveredWikiEdgeId.value = "";
    recomputeWikiLayout();
    if (!previous?.graph?.nodes?.some((node) => survivingIds.has(node.id))) fitWikiGraphView();
  }
);

// 画布跟随容器实际尺寸（1:1 viewBox，不再固定比例拉伸变形）。
let wikiResizeObserver: ResizeObserver | null = null;
let wikiResizeTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  wikiGraphPanelRef,
  (panel) => {
    wikiResizeObserver?.disconnect();
    wikiResizeObserver = null;
    if (!panel || typeof ResizeObserver === "undefined") {
      return;
    }
    wikiResizeObserver = new ResizeObserver((observed) => {
      const rect = observed[0]?.contentRect;
      if (!rect) {
        return;
      }
      const width = Math.max(320, Math.round(rect.width));
      const height = Math.max(280, Math.round(rect.height));
      if (
        Math.abs(width - wikiCanvasSize.value.width) < 4 &&
        Math.abs(height - wikiCanvasSize.value.height) < 4
      ) {
        return;
      }
      wikiCanvasSize.value = { width, height };
      if (wikiResizeTimer) {
        clearTimeout(wikiResizeTimer);
      }
      wikiResizeTimer = setTimeout(() => {
        wikiResizeTimer = null;
        recomputeWikiLayout();
        fitWikiGraphView();
      }, 160);
    });
    wikiResizeObserver.observe(panel);
  },
  { immediate: true }
);

watch(
  () => [workspaceStore.lastSavedAt, storyStore.lastLoadedAt] as const,
  ([nextSaved, nextLoaded], [prevSaved, prevLoaded]) => {
    if (!props.relationshipOnly) {
      return;
    }
    if (nextSaved === prevSaved && nextLoaded === prevLoaded) {
      return;
    }
    if (!nextSaved && !nextLoaded) {
      return;
    }
    // 保存文件或 Agent 写作后，自动做一次本地增量同步让知识图谱跟上变更。
    scheduleWikiSync();
  }
);

onBeforeUnmount(() => {
  if (wikiSyncTimer) {
    clearTimeout(wikiSyncTimer);
    wikiSyncTimer = null;
  }
  if (wikiResizeTimer) {
    clearTimeout(wikiResizeTimer);
    wikiResizeTimer = null;
  }
  wikiResizeObserver?.disconnect();
  wikiResizeObserver = null;
  stopWikiPanAnimation();
});

onMounted(() => {
  if (props.relationshipOnly) {
    void loadWiki();
    return;
  }
  void loadSnapshot();
});

defineExpose({
  loadSnapshot,
  undoLastRollback,
  __testUtils: import.meta.env.MODE === "test" ? {
    panelLoading,
    changeEntries,
    outlineChapters,
    conflictEntries,
    relationshipEdges,
    relationshipGraphNodes,
    relationshipGraphEdges,
    hasRelationshipGraphContent,
    selectedRelationshipEdge,
    selectedRelationshipNode,
    selectedRelationshipNodeInfo,
    selectedRelationshipLatestHistory,
    foreshadowThreads,
    timelineEntries,
    tabs,
    visibleTabs,
    wikiEntries,
    wikiCategoryTabs,
    selectedWikiCategoryEntries,
    visibleWikiEntries,
    wikiProjectionStatus,
    wikiRevisionLabel,
    selectedWikiEntry,
    selectedWikiCategoryLabel,
    selectedWikiNode,
    selectedWikiRelationEdge,
    wikiGraphDegrees,
    wikiIsolatedRawGraphNodes,
    wikiVisibleRawGraphNodes,
    wikiGraphNodes,
    wikiGraphEdges,
    wikiGraphFocusCenterId,
    wikiGraphFocusIds,
    visibleWikiGraphLabelEdges,
    wikiGraphLegend,
    wikiEdgeLegend,
    wikiNodeLabelLayout,
    wikiLayoutBands,
    wikiLayoutCaptions,
    wikiFocusNode,
    wikiFocusRelations,
    wikiHeaderMeta,
    wikiInspectorOpen,
    selectedWikiDetailSummary,
    selectedWikiDetailLines,
    cleanWikiSummary,
    handleWikiGraphBackgroundClick,
    selectedWikiDetailEntry,
    selectedWikiDetailKicker,
    wikiInspectorEmptyTitle,
    wikiInspectorEmptyHint,
    wikiGenerationModeLabel,
    wikiUpdatedAtLabel,
    wikiNeedsReviewCount,
    wikiGraphStats,
    wikiGraphHasMore,
    wikiGraphNextOffset,
    wikiReviewQueue,
    wikiReviewTotal,
    wikiReviewLoading,
    wikiReviewError,
    wikiReviewOpen,
    wikiReviewActionId,
    wikiReviewDrafts,
    wikiProjectSummary,
    selectedWikiSourceLabel,
    wikiWorkflowLabel,
    snapshot,
    wikiData,
    wikiAgentStatus,
    wikiAgentTone,
    wikiGraphQueryData,
    wikiGraphSearchInput,
    wikiGraphSearchQuery,
    wikiKnowledgeStatusFilter,
    wikiSyncErrorMessage,
    selectedWikiCategory,
    selectedWikiEntryId,
    selectedWikiNodeId,
    selectedWikiEdgeId,
    hoveredWikiNodeId,
    hoveredWikiEdgeId,
    wikiGraphZoom,
    wikiGraphPan,
    wikiCanvasSize,
    wikiLayoutPositions,
    wikiNodeOverrides,
    activeTab,
    selectedRelationshipIndex,
    selectedRelationshipNodeId,
    relationshipGraphZoom,
    relationshipGraphPan,
    relationshipNodeOverrides,
    wikiNodeRadius,
    wikiNodeLabel,
    resolveWikiDisplayLabel,
    wikiRelationshipSemanticsLabel,
    recomputeWikiLayout,
    isWikiNodeDimmed,
    isWikiEdgeDimmed,
    shortFieldLabel,
    formatTime,
    levelWidth,
    levelColor,
    formatRelationshipDimension,
    relationshipDimensionLabel,
    formatRelationshipNodeKind,
    normalizeRelationshipGraphText,
    relationshipNodePayloadId,
    relationshipNodePayloadLabel,
    shortNodeLabel,
    buildRelationshipEdgePath,
    buildRelationshipEdgeLabelPoint,
    buildRelationshipCurve,
    zoomRelationshipGraph,
    resetRelationshipGraphView,
    selectRelationshipEdge,
    selectRelationshipNode,
    clampNumber,
    currentWikiGraphQueryParams,
    loadWikiGraph,
    loadMoreWikiGraph,
    loadWikiReviewQueue,
    toggleWikiReviewQueue,
    openWikiSource,
    confirmWikiCandidate,
    rejectWikiCandidate,
    runWikiAgentWorkflow,
    refreshPanel,
    syncWiki,
    ensureWikiSelection,
    ensureWikiGraphSelection,
    normalizeWikiCategory,
    preferredWikiCategory,
    hasWikiRelationshipNetwork,
    selectWikiCategory,
    selectWikiEntry,
    isWikiNodeSelectable,
    selectWikiNode,
    selectWikiEdge,
    isWikiEdgeActive,
    submitWikiGraphSearch,
    clearWikiGraphSearch,
    zoomWikiGraphAt,
    zoomWikiGraphStep,
    fitWikiGraphView,
    resetWikiGraphView,
    clearWikiGraphSelection,
    wikiDetailPinned,
    wikiActiveEntryId,
    entryHasSegmentPath,
    isCharacterAssetPath
  } : null
});
</script>

<style scoped>
.story-state-panel {
  width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-sidebar);
  border-top: 1px solid var(--border-ghost);
  color: var(--text-main);
  font-size: 12px;
  box-sizing: border-box;
}
.story-state-panel.is-expanded {
  height: 100%;
  border-top: 0;
  background: var(--bg-editor);
}
/* 知识图谱页：面板本身作为容器，工具条与详情栏按面板宽度（而非窗口宽度）自适应。 */
.story-state-panel.is-relationship-only {
  container: ssp-wiki-panel / inline-size;
}
.story-state-panel.is-relationship-only .ssp-header {
  min-height: 56px;
  padding: 12px 16px 8px 18px;
}
.story-state-panel.is-relationship-only .ssp-title {
  gap: 10px;
}
.story-state-panel.is-relationship-only .ssp-title-icon {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-md);
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 17px;
}
.ssp-title-text {
  min-width: 0;
  display: grid;
  gap: 1px;
}
.story-state-panel.is-relationship-only .ssp-title-main {
  color: var(--text-main);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
  line-height: 1.3;
}
.ssp-title-meta {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 400;
  line-height: 1.4;
  white-space: nowrap;
  text-overflow: ellipsis;
  cursor: default;
}
.story-state-panel.is-relationship-only .ssp-header-actions {
  gap: 4px;
}
.story-state-panel.is-relationship-only .ssp-refresh {
  width: auto;
  min-width: 30px;
  height: 30px;
  gap: 6px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  color: var(--text-soft);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  transition: background 140ms ease, border-color 140ms ease, color 140ms ease;
}
.story-state-panel.is-relationship-only .ssp-refresh .material-symbols-rounded {
  font-size: 16px;
}
.story-state-panel.is-relationship-only .ssp-refresh:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}
.story-state-panel.is-relationship-only .ssp-rebuild {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border-subtle));
  background: var(--accent-soft);
  color: var(--accent-strong);
}
.story-state-panel.is-relationship-only .ssp-rebuild:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent) 16%, var(--surface-1));
  color: var(--accent-strong);
}
.story-state-panel.is-relationship-only .ssp-refresh:disabled {
  opacity: 0.45;
  cursor: default;
}
.ssp-header-divider {
  width: 1px;
  height: 16px;
  margin: 0 4px;
  background: var(--border-subtle);
}
.story-state-panel.is-expanded .ssp-body {
  max-height: none;
  padding: 10px 20px 14px;
}
.story-state-panel.is-relationship-only.is-expanded .ssp-body {
  padding: 4px 16px 16px;
  overflow: hidden;
}
.story-state-panel.is-expanded .ssp-relation-graph {
  height: min(68vh, 760px);
  min-height: 520px;
}
.story-state-panel.is-expanded .ssp-relation-inspector {
  gap: 16px;
}
.ssp-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 38px;
  padding: 9px 18px 7px;
  gap: 8px;
}
.ssp-title {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 700;
}
.ssp-title-icon {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: 17px;
}
.ssp-header-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.ssp-refresh {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 0;
  color: var(--text-main);
}
.ssp-refresh .material-symbols-rounded {
  font-size: 17px;
}
.ssp-refresh:hover {
  background: var(--bg-hover);
}
.ssp-rebuild {
  color: var(--accent-strong);
}
.ssp-tabs {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  padding: 0 18px 8px;
}
.ssp-tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  border: 1px solid var(--border-ghost);
  background: var(--bg-elevated);
  padding: 4px 7px;
  border-radius: var(--radius-sm);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
  color: var(--text-muted);
}
.ssp-tab.active {
  background: var(--bg-selected);
  border-color: var(--border-subtle);
  color: var(--accent-strong);
}
.ssp-tab-count {
  flex: 0 0 auto;
  min-width: 16px;
  text-align: center;
  font-size: 10px;
  background: var(--bg-hover);
  color: var(--text-faint);
  border-radius: var(--radius-full);
  padding: 0 4px;
}
.ssp-body {
  flex: 1 1 auto;
  min-height: 0;
  padding: 4px 18px 12px;
  overflow-y: auto;
  max-height: min(360px, 45vh);
}
.ssp-empty {
  padding: 12px 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.6;
}
/* ================= 知识图谱工作区 =================
   结构：工具条（分类 · 搜索 · 待确认）→ 主区（图谱舞台 | 详情栏）。
   舞台是卡片里的一块浅色内嵌层（点阵底），节点为「分类色淡底 + 同色描边 + 类型图标」，
   连线默认中性灰，聚焦时才用强调色；待确认关系统一用琥珀色虚线。
   详情栏在宽屏常驻；面板变窄时改为浮在舞台右侧的卡片，选中节点或连线时出现。 */
.ssp-wiki-workspace {
  --wiki-graph-bg: color-mix(in srgb, var(--surface-2) 64%, var(--surface-1));
  --wiki-tone-character: var(--wiki-cat-1);
  --wiki-tone-plot: var(--wiki-cat-2);
  --wiki-tone-setting: var(--wiki-cat-3);
  --wiki-tone-misc: var(--wiki-cat-4);
  --wiki-tone-hub: var(--text-muted);
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ssp-wiki-workspace .tone-character { --wiki-node-color: var(--wiki-tone-character); }
.ssp-wiki-workspace .tone-plot { --wiki-node-color: var(--wiki-tone-plot); }
.ssp-wiki-workspace .tone-setting { --wiki-node-color: var(--wiki-tone-setting); }
.ssp-wiki-workspace .tone-misc { --wiki-node-color: var(--wiki-tone-misc); }
.ssp-wiki-workspace .tone-hub { --wiki-node-color: var(--wiki-tone-hub); }

/* ---------- 工具条 ---------- */
.ssp-wiki-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 10px;
}
.ssp-wiki-category-tabs {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 34px;
  padding: 3px;
  box-sizing: border-box;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-lg);
  background: var(--canvas);
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  box-shadow: none;
  color: var(--text-muted);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease, box-shadow 140ms ease;
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab .material-symbols-rounded {
  font-size: 16px;
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab small {
  min-width: 18px;
  padding: 0 5px;
  box-sizing: border-box;
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  color: var(--text-faint);
  font-size: 10px;
  font-weight: 600;
  line-height: 16px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab:hover:not(.active) {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab.active {
  background: var(--nav-selected-bg);
  color: var(--text-main);
  font-weight: 600;
  box-shadow: var(--nav-selected-shadow);
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab.active .material-symbols-rounded {
  color: var(--accent);
}
.ssp-wiki-category-tabs .ssp-wiki-category-tab.active small {
  background: var(--accent-soft);
  color: var(--accent-strong);
}
.ssp-wiki-search-form {
  flex: 1 1 200px;
  max-width: 340px;
  height: 34px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  padding: 0 6px 0 10px;
  background: var(--surface-1);
  transition: border-color 140ms ease, box-shadow 140ms ease;
}
.ssp-wiki-search-form:hover {
  border-color: var(--border-strong);
}
.ssp-wiki-search-form:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.ssp-wiki-search-form > .material-symbols-rounded {
  flex: 0 0 auto;
  color: var(--text-faint);
  font-size: 17px;
}
.ssp-wiki-search-input {
  flex: 1 1 auto;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 13px;
}
.ssp-wiki-search-input::placeholder {
  color: var(--text-faint);
}
.ssp-wiki-search-input::-webkit-search-cancel-button {
  display: none;
}
.ssp-wiki-search-clear {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.ssp-wiki-search-clear:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-search-clear .material-symbols-rounded {
  font-size: 15px;
}
.ssp-wiki-toolbar-actions {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ssp-wiki-review-alert,
.ssp-wiki-inspector-toggle {
  height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-soft);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease, color 140ms ease;
}
.ssp-wiki-review-alert {
  padding: 0 10px 0 8px;
  font-variant-numeric: tabular-nums;
}
.ssp-wiki-review-alert-count {
  font-weight: 600;
}
.ssp-wiki-review-alert .material-symbols-rounded,
.ssp-wiki-inspector-toggle .material-symbols-rounded {
  font-size: 16px;
}
.ssp-wiki-review-alert:hover,
.ssp-wiki-inspector-toggle:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-review-alert.has-pending {
  border-color: var(--warning-border);
  background: var(--warning-bg);
  color: var(--warning-fg);
  font-weight: 600;
}
.ssp-wiki-review-alert.has-pending:hover {
  border-color: var(--warning-fg);
  background: var(--warning-bg);
  color: var(--warning-fg);
}
.ssp-wiki-review-alert.active {
  box-shadow: 0 0 0 2px var(--accent-soft);
}
.ssp-wiki-inspector-toggle {
  display: none;
  width: 30px;
  justify-content: center;
  padding: 0;
}
.ssp-wiki-inspector-toggle.active {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border-subtle));
  background: var(--bg-selected);
  color: var(--accent-strong);
}

/* ---------- 提示条 ---------- */
.ssp-wiki-evidence-note {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
  font-style: italic;
}
.ssp-wiki-agent-status {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--info-border);
  border-radius: var(--radius-md);
  padding: 5px 6px 5px 10px;
  color: var(--info-fg);
  background: var(--info-bg);
  font-size: 12px;
  line-height: 1.5;
}
.ssp-wiki-agent-status > .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 16px;
}
.ssp-wiki-agent-status-text {
  flex: 1 1 auto;
  min-width: 0;
}
.ssp-wiki-agent-status-close {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: inherit;
  opacity: 0.7;
  cursor: pointer;
}
.ssp-wiki-agent-status-close:hover {
  opacity: 1;
  background: color-mix(in srgb, currentColor 10%, transparent);
}
.ssp-wiki-agent-status-close .material-symbols-rounded {
  font-size: 15px;
}
.ssp-wiki-agent-status.success {
  border-color: var(--success-border);
  background: var(--success-bg);
  color: var(--success-fg);
}
.ssp-wiki-agent-status.warning {
  border-color: var(--warning-border);
  background: var(--warning-bg);
  color: var(--warning-fg);
}
.ssp-wiki-agent-status.error {
  border-color: var(--danger-border);
  background: var(--danger-bg);
  color: var(--danger-fg);
}

/* ---------- 主区：舞台 | 详情栏 ---------- */
.ssp-wiki-main {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 12px;
}

/* ---------- 图谱舞台 ---------- */
.ssp-wiki-graph-panel {
  position: relative;
  min-width: 0;
  min-height: 0;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background-color: var(--wiki-graph-bg);
  background-image: radial-gradient(color-mix(in srgb, var(--text-faint) 22%, transparent) 1px, transparent 1.4px);
  background-size: 24px 24px;
  background-position: 12px 12px;
}
.ssp-wiki-graph-panel.is-busy::before {
  content: "";
  position: absolute;
  z-index: 3;
  top: 0;
  left: 0;
  width: 32%;
  height: 2px;
  background: var(--accent);
  animation: ssp-wiki-busy 1.1s ease-in-out infinite;
}
@keyframes ssp-wiki-busy {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(320%); }
}
.ssp-wiki-graph {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  touch-action: none;
  cursor: grab;
  user-select: none;
}
.ssp-wiki-graph:active {
  cursor: grabbing;
}

/* 分区标题：「章节顺序 · 6」「独立条目 · 4」，细线延伸到图的右缘 */
.ssp-wiki-band-caption {
  pointer-events: none;
}
.ssp-wiki-band-caption text {
  fill: var(--text-faint);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.ssp-wiki-band-caption line {
  stroke: color-mix(in srgb, var(--text-faint) 30%, transparent);
  stroke-width: 1;
  stroke-dasharray: 2 4;
}

/* ---------- 连线 ---------- */
.ssp-wiki-edge-group {
  cursor: pointer;
  transition: opacity 160ms ease;
}
.ssp-wiki-edge-group.dimmed {
  opacity: 0.07;
  pointer-events: none;
}
.ssp-wiki-edge {
  fill: none;
  stroke: color-mix(in srgb, var(--text-muted) 36%, transparent);
  stroke-width: 1.2;
  stroke-linecap: round;
  transition: stroke 140ms ease, stroke-width 140ms ease;
}
.ssp-wiki-edge.edge-real-relation {
  stroke: color-mix(in srgb, var(--text-soft) 50%, transparent);
  stroke-width: var(--wiki-edge-width, 1.5);
}
.ssp-wiki-edge.edge-timeline {
  stroke: color-mix(in srgb, var(--wiki-tone-plot) 52%, transparent);
  stroke-width: 1.6;
}
.ssp-wiki-edge.edge-co-occurrence {
  stroke-dasharray: 2 5;
}
.ssp-wiki-edge.edge-review-required {
  stroke: color-mix(in srgb, var(--warning-fg) 78%, transparent);
  stroke-dasharray: 5 4;
  stroke-width: 1.5;
}
.ssp-wiki-edge-group:hover .ssp-wiki-edge,
.ssp-wiki-edge-group.hovered .ssp-wiki-edge {
  stroke: var(--accent);
  stroke-width: 2.2;
}
.ssp-wiki-edge.active {
  stroke: var(--accent-strong);
  stroke-width: 2.4;
}
.ssp-wiki-edge-group:hover .ssp-wiki-edge.edge-review-required,
.ssp-wiki-edge-group.hovered .ssp-wiki-edge.edge-review-required,
.ssp-wiki-edge.edge-review-required.active {
  stroke: var(--warning-fg);
  stroke-width: 2.2;
}
.ssp-wiki-edge-arrow {
  fill: color-mix(in srgb, var(--text-soft) 58%, transparent);
  transition: fill 140ms ease;
}
.ssp-wiki-edge.edge-timeline ~ .ssp-wiki-edge-arrow {
  fill: color-mix(in srgb, var(--wiki-tone-plot) 62%, transparent);
}
.ssp-wiki-edge.edge-review-required ~ .ssp-wiki-edge-arrow {
  fill: color-mix(in srgb, var(--warning-fg) 82%, transparent);
}
.ssp-wiki-edge-group:hover .ssp-wiki-edge-arrow,
.ssp-wiki-edge-group.hovered .ssp-wiki-edge-arrow {
  fill: var(--accent);
}
.ssp-wiki-edge.active ~ .ssp-wiki-edge-arrow {
  fill: var(--accent-strong);
}
.ssp-wiki-edge-group:hover .ssp-wiki-edge.edge-review-required ~ .ssp-wiki-edge-arrow,
.ssp-wiki-edge-group.hovered .ssp-wiki-edge.edge-review-required ~ .ssp-wiki-edge-arrow,
.ssp-wiki-edge.edge-review-required.active ~ .ssp-wiki-edge-arrow {
  fill: var(--warning-fg);
}
/* 透明的粗命中区：细线也容易点中 */
.ssp-wiki-edge-hit {
  fill: none;
  stroke: transparent;
  stroke-width: 12;
  stroke-linecap: round;
}

/* 关系名：小胶囊，平时克制，hover/选中时与连线一起高亮 */
.ssp-wiki-edge-label {
  cursor: pointer;
}
.ssp-wiki-edge-label rect {
  fill: var(--surface-1);
  stroke: var(--border-subtle);
  stroke-width: 1;
  transition: fill 140ms ease, stroke 140ms ease;
}
.ssp-wiki-edge-label text {
  fill: var(--text-soft);
  font-size: 11px;
  font-weight: 500;
  pointer-events: none;
}
.ssp-wiki-edge-label.is-review rect {
  fill: color-mix(in srgb, var(--warning-fg) 9%, var(--surface-1));
  stroke: var(--warning-border);
}
.ssp-wiki-edge-label.is-review text {
  fill: var(--warning-fg);
}
.ssp-wiki-edge-label:hover rect,
.ssp-wiki-edge-label.hovered rect,
.ssp-wiki-edge-label.active rect {
  fill: color-mix(in srgb, var(--accent) 10%, var(--surface-1));
  stroke: color-mix(in srgb, var(--accent) 55%, var(--border-subtle));
}
.ssp-wiki-edge-label:hover text,
.ssp-wiki-edge-label.hovered text,
.ssp-wiki-edge-label.active text {
  fill: var(--accent-strong);
  font-weight: 600;
}
.ssp-wiki-edge-label.is-review:hover rect,
.ssp-wiki-edge-label.is-review.hovered rect,
.ssp-wiki-edge-label.is-review.active rect {
  fill: color-mix(in srgb, var(--warning-fg) 16%, var(--surface-1));
  stroke: var(--warning-fg);
}
.ssp-wiki-edge-label.is-review:hover text,
.ssp-wiki-edge-label.is-review.hovered text,
.ssp-wiki-edge-label.is-review.active text {
  fill: var(--warning-fg);
}

/* ---------- 节点：分类色淡底 + 同色描边 + 类型图标 ---------- */
.ssp-wiki-node {
  --wiki-node-color: var(--text-muted);
  cursor: default;
  transition: opacity 160ms ease;
}
.ssp-wiki-node.is-selectable {
  cursor: pointer;
}
.ssp-wiki-node-halo {
  fill: transparent;
  stroke: transparent;
  stroke-width: 2;
  transition: fill 140ms ease, stroke 140ms ease;
}
.ssp-wiki-node-dot {
  fill: color-mix(in srgb, var(--wiki-node-color) 15%, var(--surface-1));
  stroke: color-mix(in srgb, var(--wiki-node-color) 72%, var(--surface-1));
  stroke-width: 1.5;
  transition: fill 140ms ease, stroke 140ms ease;
}
.ssp-wiki-node-glyph {
  fill: var(--wiki-node-color);
  font-family: "Material Symbols Rounded", sans-serif;
  font-weight: 400;
  font-feature-settings: "liga";
  pointer-events: none;
  transition: fill 140ms ease;
}
:root:not(.icon-font-ready) .ssp-wiki-node-glyph,
:root.icon-font-failed .ssp-wiki-node-glyph {
  visibility: hidden;
}
.ssp-wiki-node-badge {
  fill: var(--warning-fg);
  stroke: var(--wiki-graph-bg);
  stroke-width: 2;
}
/* 节点名：用舞台底色描边，压在连线上也清楚 */
.ssp-wiki-node-label {
  fill: var(--text-soft);
  font-size: 12px;
  font-weight: 500;
  paint-order: stroke;
  stroke: var(--wiki-graph-bg);
  stroke-width: 4px;
  stroke-linejoin: round;
  pointer-events: none;
  transition: fill 140ms ease;
}
.ssp-wiki-node:hover .ssp-wiki-node-dot {
  fill: color-mix(in srgb, var(--wiki-node-color) 24%, var(--surface-1));
  stroke: var(--wiki-node-color);
}
.ssp-wiki-node:hover .ssp-wiki-node-halo {
  fill: color-mix(in srgb, var(--wiki-node-color) 10%, transparent);
}
.ssp-wiki-node:hover .ssp-wiki-node-label,
.ssp-wiki-node.active .ssp-wiki-node-label {
  fill: var(--text-main);
  font-weight: 600;
}
.ssp-wiki-node.active .ssp-wiki-node-dot {
  fill: var(--wiki-node-color);
  stroke: var(--wiki-node-color);
}
.ssp-wiki-node.active .ssp-wiki-node-glyph {
  fill: var(--surface-1);
}
.ssp-wiki-node.active .ssp-wiki-node-halo {
  fill: color-mix(in srgb, var(--wiki-node-color) 14%, transparent);
  stroke: color-mix(in srgb, var(--wiki-node-color) 42%, transparent);
}
.ssp-wiki-node.dimmed {
  opacity: 0.16;
}
.ssp-wiki-node.is-neighbor {
  opacity: 0.72;
}
.ssp-wiki-node.is-neighbor.dimmed {
  opacity: 0.1;
}
.ssp-wiki-node.is-neighbor .ssp-wiki-node-label,
.ssp-wiki-node.band-shelf .ssp-wiki-node-label {
  font-size: 11px;
  fill: var(--text-muted);
}
.ssp-wiki-node.band-shelf:hover .ssp-wiki-node-label,
.ssp-wiki-node.band-shelf.active .ssp-wiki-node-label {
  fill: var(--text-main);
}
.ssp-wiki-node.is-synthetic .ssp-wiki-node-dot {
  fill: var(--surface-1);
  stroke-dasharray: 3 3;
}

/* ---------- 舞台上的悬浮控件 ---------- */
.ssp-wiki-graph-note {
  position: absolute;
  z-index: 2;
  left: 50%;
  top: 12px;
  transform: translateX(-50%);
  max-width: calc(100% - 32px);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 4px 4px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: var(--surface-1);
  box-shadow: var(--shadow-raised);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.ssp-wiki-graph-note button {
  height: 22px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--radius-full);
  background: var(--accent-soft);
  color: var(--accent-strong);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}
.ssp-wiki-graph-note button:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 22%, transparent);
}
.ssp-wiki-graph-note button:disabled {
  opacity: 0.55;
  cursor: wait;
}
.ssp-wiki-graph-error {
  position: absolute;
  z-index: 2;
  left: 12px;
  right: 12px;
  top: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-md);
  background: var(--danger-bg);
  color: var(--danger-fg);
  font-size: 12px;
  line-height: 1.5;
}
.ssp-wiki-graph-error .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 16px;
}
.ssp-wiki-graph-legend {
  position: absolute;
  z-index: 2;
  left: 12px;
  bottom: 12px;
  max-width: calc(100% - 196px);
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 12px;
  min-height: 30px;
  box-sizing: border-box;
  padding: 5px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: var(--surface-1);
  box-shadow: var(--shadow-raised);
  color: var(--text-soft);
  font-size: 11px;
  line-height: 1.5;
  pointer-events: none;
  font-variant-numeric: tabular-nums;
}
.ssp-wiki-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.ssp-wiki-legend-item i {
  width: 9px;
  height: 9px;
  box-sizing: border-box;
  border: 1.5px solid var(--wiki-node-color, var(--text-muted));
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--wiki-node-color, var(--text-muted)) 22%, var(--surface-1));
}
.ssp-wiki-legend-divider {
  width: 1px;
  height: 12px;
  background: var(--border-subtle);
}
.ssp-wiki-legend-edge svg {
  width: 18px;
  height: 6px;
  overflow: visible;
}
.ssp-wiki-legend-edge line {
  stroke: color-mix(in srgb, var(--text-soft) 55%, transparent);
  stroke-width: 1.6;
  stroke-linecap: round;
}
.ssp-wiki-legend-edge.edge-review line {
  stroke: var(--warning-fg);
  stroke-dasharray: 4 3;
}
.ssp-wiki-legend-edge.edge-structural line {
  stroke: color-mix(in srgb, var(--wiki-tone-plot) 60%, transparent);
}
.ssp-wiki-graph-hud {
  position: absolute;
  z-index: 2;
  right: 12px;
  bottom: 12px;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 30px;
  box-sizing: border-box;
  padding: 0 3px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: var(--surface-1);
  box-shadow: var(--shadow-raised);
}
.ssp-hud-btn {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.ssp-hud-btn:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-hud-btn .material-symbols-rounded {
  font-size: 16px;
}
.ssp-hud-zoom {
  min-width: 40px;
  text-align: center;
  color: var(--text-soft);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.ssp-hud-divider {
  width: 1px;
  height: 14px;
  margin: 0 2px;
  background: var(--border-subtle);
}
.ssp-wiki-graph-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 8px;
  padding: 24px;
  color: var(--text-muted);
  text-align: center;
}
.ssp-wiki-graph-empty > .material-symbols-rounded {
  width: 52px;
  height: 52px;
  display: grid;
  place-items: center;
  margin-bottom: 4px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: var(--surface-1);
  box-shadow: var(--shadow-resting);
  font-size: 26px;
  color: var(--text-faint);
}
.ssp-wiki-graph-empty.is-error > .material-symbols-rounded {
  border-color: var(--danger-border);
  background: var(--danger-bg);
  color: var(--danger-fg);
}
.ssp-wiki-graph-empty .is-spinning {
  animation: spin-ring 1s linear infinite;
}
.ssp-wiki-graph-empty p {
  margin: 0;
  max-width: 420px;
  color: var(--text-main);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
}
.ssp-wiki-graph-empty small {
  max-width: 340px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.6;
}
.ssp-wiki-empty-action {
  margin-top: 6px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.ssp-wiki-empty-action:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}
.ssp-wiki-empty-action .material-symbols-rounded {
  font-size: 15px;
}

/* ---------- 详情栏 ---------- */
.ssp-wiki-inspector {
  position: relative;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--surface-1);
}
.ssp-wiki-inspector-close {
  display: none;
  position: absolute;
  z-index: 2;
  top: 10px;
  right: 10px;
  width: 26px;
  height: 26px;
  place-items: center;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.ssp-wiki-inspector-close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-inspector-close .material-symbols-rounded {
  font-size: 17px;
}
.ssp-wiki-inspector-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.ssp-wiki-inspector-detail {
  padding: 16px 18px 18px;
  border-bottom: 1px solid var(--border-ghost);
}
.ssp-wiki-detail-kicker-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 22px;
  padding-right: 28px;
}
.ssp-wiki-entry-kicker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  padding: 0 9px;
  box-sizing: border-box;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-full);
  background: var(--surface-2);
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 600;
}
.ssp-wiki-entry-kicker i,
.ssp-wiki-endpoint i,
.ssp-wiki-relation-other i,
.ssp-wiki-entry-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  box-sizing: border-box;
  border: 1.5px solid var(--wiki-node-color, var(--text-muted));
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--wiki-node-color, var(--text-muted)) 24%, var(--surface-1));
}
.ssp-wiki-inspector-detail h3 {
  margin: 10px 0 6px;
  color: var(--text-main);
  font-size: 17px;
  font-weight: 600;
  line-height: 1.4;
  letter-spacing: -0.01em;
  overflow-wrap: anywhere;
}
.ssp-wiki-inspector-detail p {
  margin: 0;
  color: var(--text-soft);
  font-size: 13px;
  line-height: 1.7;
}
.ssp-wiki-inspector-detail p + p {
  margin-top: 8px;
}
.ssp-wiki-inspector-detail .ssp-wiki-detail-subtitle {
  margin: -2px 0 10px;
  color: var(--text-muted);
  font-size: 12px;
}
.ssp-wiki-detail-lines {
  margin: 10px 0 0;
  padding-left: 16px;
}
.ssp-wiki-detail-lines li {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.7;
}
.ssp-wiki-detail-lines li::marker {
  color: var(--text-faint);
}
.ssp-wiki-inspector-detail > small,
.ssp-wiki-review-card > small {
  display: block;
  margin-top: 10px;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.6;
}
.ssp-wiki-detail-section {
  margin-top: 16px;
}
.ssp-wiki-section-label {
  margin: 0 0 6px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.ssp-wiki-relation-row {
  width: calc(100% + 12px);
  margin: 0 -6px;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 4px 6px;
  box-sizing: border-box;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.ssp-wiki-relation-row:hover {
  background: var(--bg-hover);
}
.ssp-wiki-relation-row > .material-symbols-rounded {
  flex: 0 0 auto;
  color: var(--text-faint);
  font-size: 14px;
}
.ssp-wiki-relation-other {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ssp-wiki-relation-predicate {
  min-width: 0;
  margin-left: auto;
  color: var(--text-soft);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ssp-wiki-relation-predicate small {
  color: var(--text-faint);
  font-size: 11px;
}
.ssp-wiki-status-chip {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 8px;
  box-sizing: border-box;
  border: 1px solid var(--success-border);
  border-radius: var(--radius-full);
  background: var(--success-bg);
  color: var(--success-fg);
  font-size: 10.5px;
  font-weight: 600;
  white-space: nowrap;
}
.ssp-wiki-status-chip.status-review_required {
  border-color: var(--warning-border);
  background: var(--warning-bg);
  color: var(--warning-fg);
}
.ssp-wiki-status-chip.status-rejected {
  border-color: var(--danger-border);
  background: var(--danger-bg);
  color: var(--danger-fg);
}
.ssp-wiki-relation-endpoints {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 12px;
}
.ssp-wiki-endpoint {
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  box-sizing: border-box;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: var(--surface-2);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.ssp-wiki-endpoint:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}
.ssp-wiki-relation-endpoints > .material-symbols-rounded {
  color: var(--text-faint);
  font-size: 16px;
}
.ssp-wiki-relation-level {
  display: grid;
  gap: 5px;
  margin: 0 0 12px;
  color: var(--text-muted);
  font-size: 11px;
}
.ssp-wiki-source-ref-list {
  margin-top: 14px;
}
.ssp-wiki-source-ref {
  display: grid;
  gap: 4px;
  margin-top: 8px;
}
.ssp-wiki-source-ref blockquote {
  margin: 0;
  padding: 8px 10px;
  border-left: 3px solid color-mix(in srgb, var(--accent) 45%, var(--border-subtle));
  border-radius: 0 var(--radius-md) var(--radius-md) 0;
  background: var(--surface-2);
  color: var(--text-main);
  font-family: var(--font-prose);
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.ssp-wiki-source-link {
  justify-self: start;
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 0;
  background: transparent;
  padding: 2px 0;
  color: var(--text-muted);
  font: inherit;
  font-size: 11px;
  text-align: left;
  cursor: pointer;
}
.ssp-wiki-source-link > span:last-child {
  min-width: 0;
  overflow-wrap: anywhere;
}
.ssp-wiki-source-link .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 13px;
}
.ssp-wiki-source-link:hover {
  color: var(--accent-strong);
}
.ssp-wiki-source-link:hover > span:last-child {
  text-decoration: underline;
}
.ssp-wiki-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ssp-wiki-source-chip {
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px 0 7px;
  box-sizing: border-box;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-full);
  background: var(--surface-2);
  color: var(--text-muted);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}
.ssp-wiki-source-chip > span:last-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssp-wiki-source-chip .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 13px;
}
.ssp-wiki-source-chip:hover {
  border-color: var(--border-subtle);
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-review-inline-actions {
  display: flex;
  gap: 8px;
  margin-top: 14px;
}
.ssp-wiki-entry-kicker .material-symbols-rounded {
  margin-left: -2px;
  font-size: 14px;
}
.ssp-wiki-inspector-detail .ssp-wiki-overview-hint {
  margin-top: 10px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.65;
}
.ssp-wiki-review-chip {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 8px;
  box-sizing: border-box;
  border: 1px solid var(--warning-border);
  border-radius: var(--radius-full);
  color: var(--warning-fg);
  background: var(--warning-bg);
  font-size: 10.5px;
  font-weight: 600;
}

/* 分段标题：「角色条目 · 9」「关系待确认 · 3」 */
.ssp-wiki-inspector-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  padding: 0 4px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.ssp-wiki-review-refresh {
  height: 22px;
  margin-left: auto;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--surface-1);
  color: var(--text-soft);
  font: inherit;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
}
.ssp-wiki-review-refresh:hover {
  border-color: var(--border-strong);
  color: var(--text-main);
}

/* ---------- 审核队列 ---------- */
.ssp-wiki-review-queue {
  padding: 14px 12px;
  border-bottom: 1px solid var(--border-ghost);
  background: color-mix(in srgb, var(--warning-bg) 40%, var(--surface-1));
}
.ssp-wiki-review-card {
  display: grid;
  gap: 10px;
  margin-top: 10px;
  padding: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--surface-1);
}
.ssp-wiki-review-card-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  color: var(--text-main);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.ssp-wiki-review-card-title > .material-symbols-rounded {
  color: var(--text-faint);
  font-size: 15px;
}
.ssp-wiki-review-card-title .ssp-wiki-status-chip {
  margin-left: auto;
}
.ssp-wiki-review-card p {
  margin: 0;
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.6;
}
.ssp-wiki-review-card > small {
  margin-top: 0;
}
.ssp-wiki-review-card .ssp-wiki-source-ref {
  margin-top: 0;
}
.ssp-wiki-review-field-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px;
}
.ssp-wiki-review-field {
  display: grid;
  gap: 4px;
  min-width: 0;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 500;
}
.ssp-wiki-review-field input,
.ssp-wiki-review-field select,
.ssp-wiki-review-field textarea {
  width: 100%;
  min-height: 30px;
  box-sizing: border-box;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-main);
  padding: 5px 8px;
  font: inherit;
  font-size: 12px;
}
.ssp-wiki-review-field input:focus,
.ssp-wiki-review-field select:focus,
.ssp-wiki-review-field textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.ssp-wiki-review-field textarea {
  resize: vertical;
}
.ssp-wiki-review-reject {
  display: grid;
  gap: 8px;
  padding: 10px;
  border-radius: var(--radius-md);
  background: var(--surface-2);
}
.ssp-wiki-review-card .ssp-action-row,
.ssp-wiki-review-inline-actions {
  margin-top: 0;
}
.ssp-wiki-review-card .ssp-btn,
.ssp-wiki-review-inline-actions .ssp-btn {
  min-height: 28px;
  padding: 0 14px;
  border-radius: var(--radius-md);
  font-size: 12px;
  font-weight: 500;
}
.ssp-wiki-review-card .ssp-btn.accept,
.ssp-wiki-review-inline-actions .ssp-btn.accept {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
}
.ssp-wiki-review-card .ssp-btn.accept:hover:not(:disabled),
.ssp-wiki-review-inline-actions .ssp-btn.accept:hover:not(:disabled) {
  background: var(--accent-strong);
}
.ssp-wiki-review-card .ssp-btn.dismiss,
.ssp-wiki-review-inline-actions .ssp-btn.dismiss {
  opacity: 1;
  border-color: var(--border-subtle);
  background: var(--surface-1);
  color: var(--text-main);
}
.ssp-wiki-review-card .ssp-btn.dismiss:hover:not(:disabled),
.ssp-wiki-review-inline-actions .ssp-btn.dismiss:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}
.ssp-wiki-review-empty,
.ssp-wiki-review-error {
  padding: 10px 4px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}
.ssp-wiki-review-error {
  color: var(--danger-fg);
}
.ssp-wiki-sidecar-note {
  color: var(--text-faint);
  font-size: 11px;
}

/* ---------- 条目列表 ---------- */
.ssp-wiki-entry-list {
  padding: 14px 10px 12px;
}
.ssp-wiki-entry-button {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 2px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  padding: 8px 10px;
  box-sizing: border-box;
  color: var(--text-main);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease;
}
.ssp-wiki-entry-dot {
  margin-top: 5px;
}
.ssp-wiki-entry-text {
  flex: 1 1 auto;
  min-width: 0;
  display: grid;
  gap: 2px;
}
.ssp-wiki-entry-text strong {
  font-size: 13px;
  font-weight: 600;
}
.ssp-wiki-entry-text span {
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}
.ssp-wiki-entry-button > small {
  flex: 0 0 auto;
  margin-top: 1px;
}
.ssp-wiki-entry-button:hover {
  background: var(--bg-hover);
}
.ssp-wiki-entry-button.active {
  border-color: var(--border-subtle);
  background: var(--bg-selected);
}

/* ---------- 宽度自适应：面板变窄时详情栏浮到舞台上 ---------- */
@container ssp-wiki-panel (max-width: 1080px) {
  .ssp-wiki-main {
    grid-template-columns: minmax(0, 1fr);
  }
  .ssp-wiki-inspector-toggle {
    display: inline-flex;
  }
  .ssp-wiki-inspector {
    position: absolute;
    z-index: 4;
    top: 10px;
    right: 10px;
    bottom: 54px;
    width: min(340px, calc(100% - 20px));
    border-color: var(--border-subtle);
    box-shadow: var(--shadow-overlay);
    opacity: 0;
    visibility: hidden;
    transform: translateX(8px);
    transition: opacity 160ms ease, transform 160ms ease, visibility 0s linear 160ms;
  }
  .ssp-wiki-inspector.is-open {
    opacity: 1;
    visibility: visible;
    transform: none;
    transition: opacity 160ms ease, transform 160ms ease;
  }
  .ssp-wiki-inspector-close {
    display: grid;
  }
}

@container ssp-wiki-panel (max-width: 860px) {
  .ssp-action-label {
    display: none;
  }
  .story-state-panel.is-relationship-only .ssp-refresh {
    width: 30px;
    padding: 0;
  }
}

@container ssp-wiki-panel (max-width: 640px) {
  .ssp-wiki-category-name,
  .ssp-wiki-legend-edge,
  .ssp-wiki-legend-divider {
    display: none;
  }
  .ssp-wiki-search-form {
    max-width: none;
  }
  .ssp-wiki-review-alert-label {
    display: none;
  }
  .ssp-wiki-review-alert {
    padding: 0 8px;
  }
}

.ssp-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ssp-list-item {
  background: var(--bg-elevated);
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-sm);
  padding: 8px;
}
.ssp-list-item.resolved {
  opacity: 0.6;
}
.ssp-list-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 12px;
}
.ssp-tag {
  min-width: 0;
  font-weight: 600;
  background: var(--bg-hover);
  color: var(--text-main);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssp-tag.status-open {
  color: var(--warning-fg);
}
.ssp-tag.status-recalled {
  color: var(--info-fg);
}
.ssp-tag.status-resolved {
  color: var(--success-fg);
}
.ssp-time {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--text-muted);
}
.ssp-snapshot {
  font-size: 12px;
  margin: 2px 0 4px;
  line-height: 1.5;
  color: var(--text-main);
  overflow-wrap: anywhere;
}
.ssp-sizes {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.ssp-size-pill {
  font-size: 11px;
  background: var(--bg-hover);
  color: var(--text-muted);
  border-radius: var(--radius-md);
  padding: 1px 6px;
}
.ssp-error {
  font-size: 11px;
  color: var(--danger-fg);
  margin-top: 4px;
}
.ssp-section-head {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  margin: 6px 0 2px;
}
.ssp-mini-list {
  list-style: disc inside;
  padding-left: 4px;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-main);
}
.ssp-mini-meta {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.ssp-mini-meta.resolved {
  color: var(--success-fg);
}
.ssp-conflict-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 4px;
}
.ssp-conflict-cell {
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  padding: 4px 6px;
}
.ssp-cell-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  margin-bottom: 2px;
}
.ssp-cell-body {
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.ssp-action-row {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}
.ssp-btn {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-ghost);
  background: transparent;
  cursor: pointer;
  color: inherit;
}
.ssp-btn.accept {
  border-color: var(--success-border);
  color: var(--success-fg);
}
.ssp-btn.keep {
  border-color: var(--info-border);
  color: var(--info-fg);
}
.ssp-btn.dismiss {
  opacity: 0.7;
}
.ssp-btn.rollback {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-color: var(--warning-border);
  color: var(--warning-fg);
}
.ssp-btn.rollback .material-symbols-rounded {
  font-size: 14px;
}
.ssp-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.ssp-resolved-tag {
  font-size: 11px;
  color: var(--success-fg);
  margin-top: 4px;
}
.ssp-level-bar {
  height: 6px;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin: 4px 0 2px;
}
.ssp-level-fill {
  height: 100%;
}
.ssp-relation-inspector {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 8px;
}
.ssp-relation-content {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, 260px);
  gap: 12px;
  align-items: stretch;
}
.ssp-relation-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.ssp-relation-mode {
  min-width: 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssp-relation-controls {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}
.ssp-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 23px;
  height: 23px;
  padding: 0;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  color: var(--text-muted);
  cursor: pointer;
}
.ssp-icon-btn:hover {
  background: var(--bg-hover);
  color: var(--accent-strong);
}
.ssp-icon-btn .material-symbols-rounded {
  font-size: 16px;
}
.ssp-zoom-label {
  min-width: 34px;
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
}
.ssp-relation-graph {
  width: 100%;
  height: 220px;
  min-height: 220px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background:
    linear-gradient(color-mix(in srgb, var(--border-subtle) 54%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--border-subtle) 54%, transparent) 1px, transparent 1px),
    var(--bg-card);
  background-size: 28px 28px;
  touch-action: none;
}
.ssp-relation-edge {
  fill: none;
  stroke: color-mix(in srgb, var(--text-muted) 52%, transparent);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 7 9;
  vector-effect: non-scaling-stroke;
  pointer-events: stroke;
  cursor: pointer;
}
.ssp-relation-edge.selected {
  stroke: var(--accent-strong);
  stroke-width: 2.4;
  stroke-dasharray: none;
}
.ssp-relation-edge-label {
  cursor: pointer;
  pointer-events: all;
}
.ssp-relation-edge-label rect {
  fill: var(--bg-card);
  stroke: var(--border-ghost);
  stroke-width: 1;
}
.ssp-relation-edge-label.selected rect {
  fill: var(--bg-selected);
  stroke: var(--accent-strong);
}
.ssp-relation-edge-label text {
  fill: var(--text-muted);
  font-family: var(--font-ui);
  font-size: 14px;
  font-weight: 600;
}
.ssp-relation-node {
  cursor: grab;
}
.ssp-relation-node:active {
  cursor: grabbing;
}
.ssp-relation-node circle {
  fill: var(--bg-card);
  stroke: var(--text-muted);
  stroke-width: 1.8;
  filter: drop-shadow(0 2px 4px rgba(17, 24, 39, 0.08));
}
.ssp-relation-node.active circle {
  fill: var(--bg-selected);
  stroke: var(--accent-strong);
  stroke-width: 3;
}
.ssp-relation-node text {
  fill: var(--text-main);
  font-family: var(--font-ui);
  font-size: 15px;
  font-weight: 700;
  pointer-events: none;
}
.ssp-relation-side-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.ssp-relation-evidence {
  flex: 1 1 auto;
  background: var(--bg-elevated);
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-sm);
  padding: 10px 10px 12px;
  min-height: 0;
  overflow: auto;
}
.ssp-side-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-soft);
  margin-bottom: 8px;
}
.ssp-character-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-main);
  margin-bottom: 10px;
  overflow-wrap: anywhere;
}
.ssp-side-fields {
  display: grid;
  gap: 7px;
  margin: 0;
}
.ssp-side-fields dt,
.ssp-side-kv span {
  font-size: 11px;
  color: var(--text-muted);
}
.ssp-side-fields dd {
  margin: 1px 0 0;
  font-size: 12px;
  line-height: 1.45;
  color: var(--text-main);
  overflow-wrap: anywhere;
}
.ssp-side-kv {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin: 7px 0;
}
.ssp-side-kv strong {
  font-size: 12px;
  color: var(--text-main);
}
.ssp-relation-inspector + .ssp-list {
  display: none;
}

@media (max-width: 720px) {
  .story-state-panel.is-expanded .ssp-body {
    padding: 12px 12px 14px;
  }

  .story-state-panel.is-expanded .ssp-relation-graph {
    height: 58vh;
    min-height: 420px;
  }

  .ssp-relation-content {
    grid-template-columns: 1fr;
  }

  .ssp-relation-side-panel {
    min-height: 150px;
  }

  .ssp-relation-toolbar {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .ssp-relation-edge-label text {
    font-size: 12px;
  }

  .ssp-relation-node text {
    font-size: 13px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ssp-wiki-graph-panel.is-busy::before,
  .ssp-wiki-graph-empty .is-spinning {
    animation: none;
  }
  .ssp-wiki-inspector,
  .ssp-wiki-inspector.is-open {
    transform: none;
  }
}
</style>
