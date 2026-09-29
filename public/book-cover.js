/* Shared, text-only cover renderer. No audio or body-page state lives here. */
(()=>{
  const defaults={title:'凡凡 land 涵涵',subtitle:'我们的故事，从这里开始',dateText:'2026',footerText:'把喜欢的瞬间，慢慢装订成一本书。',photoId:null,layout:'classic',titleAlign:'center',photoPosition:'center',showPhoto:false};
  const templates={classic:'经典纪念册',photo:'照片主视觉',minimal:'极简文字',polaroid:'相纸纪念风'};
  function render(root,value={},photo=null){
    const config={...defaults,...value},layout=templates[config.layout]?config.layout:'classic';
    const previous=root.querySelector('img');
    root.className='album-cover-card';root.dataset.layout=layout;root.dataset.align=config.titleAlign;root.dataset.photoPosition=config.photoPosition;root.dataset.photoUnavailable='0';
    root.replaceChildren();
    const ornament=document.createElement('span');ornament.className='album-cover-mark';ornament.textContent='♡';ornament.setAttribute('aria-hidden','true');root.append(ornament);
    for(const [field,tag,style] of [['title','h2','title'],['subtitle','p','subtitle'],['dateText','p','date'],['footerText','p','footer']]){
      if(!config[field])continue;const el=document.createElement(tag);el.className='album-cover-'+style;el.textContent=config[field];root.append(el);
    }
    const source=photo&&(photo.thumbnailSrc||photo.thumbnailUrl||photo.src);
    const hasPhoto=Boolean(config.showPhoto&&source&&layout!=='minimal');root.classList.toggle('has-photo',hasPhoto);
    if(hasPhoto){
      const frame=document.createElement('figure');frame.className='album-cover-photo';
      const img=previous?.dataset.coverSource===source?previous:document.createElement('img');
      if(img!==previous){img.alt=photo.title||'纪念册封面照片';img.decoding='async';img.dataset.coverSource=source;img.src=source;img.addEventListener('error',()=>{
        if(!img.dataset.originalTried&&photo.src&&photo.src!==img.getAttribute('src')){img.dataset.originalTried='1';img.src=photo.src;return}
        img.dataset.unavailable='1';const current=img.closest('figure');if(current)current.hidden=true;root.classList.remove('has-photo');root.dataset.photoUnavailable='1';root.dispatchEvent(new Event('cover-photo-unavailable'));
      })}else if(img.dataset.unavailable){frame.hidden=true;root.classList.remove('has-photo');root.dataset.photoUnavailable='1'}
      frame.append(img);root.append(frame);
    }
  }
  window.BookCover=Object.freeze({defaults,templates,render});
})();
