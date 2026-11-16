package api

import (
	"fmt"
	"net/url"
	"strconv"
)

// wouldCreateCycle reports whether moving node id under newParent would
// form a cycle (moving a node into itself or into its own descendant).
func wouldCreateCycle(getParent func(id string) (string, error), id, newParent string) (bool, error) {
	if id == newParent {
		return true, nil
	}
	// 根目录（空串）没有父节点，直接判定无环
	if newParent == "" {
		return false, nil
	}
	cur := newParent
	for i := 0; i < 100; i++ { // depth guard against corrupt data
		p, err := getParent(cur)
		if err != nil {
			return false, err
		}
		if p == id {
			return true, nil
		}
		if p == "" {
			return false, nil
		}
		cur = p
	}
	return false, fmt.Errorf("parent chain too deep")
}

// collectFolderIDs returns rootID plus every descendant id (folders and
// files), breadth-first, de-duplicated.
func collectFolderIDs(getChildren func(parent string) ([]string, error), rootID string) ([]string, error) {
	ids := []string{rootID}
	queue := []string{rootID}
	seen := map[string]bool{rootID: true}
	for len(queue) > 0 {
		parent := queue[0]
		queue = queue[1:]
		children, err := getChildren(parent)
		if err != nil {
			return nil, err
		}
		for _, c := range children {
			if seen[c] {
				continue
			}
			seen[c] = true
			ids = append(ids, c)
			queue = append(queue, c)
		}
	}
	return ids, nil
}

// parsePagination parses page/page_size query params. Defaults 1/50, size cap 200.
func parsePagination(q url.Values) (page, pageSize int) {
	page, _ = strconv.Atoi(q.Get("page"))
	if page < 1 {
		page = 1
	}
	pageSize, _ = strconv.Atoi(q.Get("page_size"))
	if pageSize < 1 {
		pageSize = 50
	}
	if pageSize > 200 {
		pageSize = 200
	}
	return page, pageSize
}
